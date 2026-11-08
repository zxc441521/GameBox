# -*- coding: utf-8 -*-
"""游匣 GameBox · 单一数据源（SQLite）。

历史实现把数据分散在 config.json / games_cache.json / meta_cache.json /
data/games.json 四份副本里，靠 _on_scan_done 手工合并，并用「全量重写整份
JSON」保存任何一处改动。这导致扫描结果覆盖在线识别、评分库漏打包即全库
无分、清缓存连带清掉识别成果等问题反复出现。

本模块把所有状态收进一个 SQLite 库：

    config      键值配置（play_stats 单独拆表，避免每次全量重写）
    games       每款游戏一行，path_key 为主键；扫描 UPSERT、识别只 patch 单行
    game_meta   评分库（data/games.json 的落库副本，外部文件缺失也不会丢分）
    steam_meta  Steam 商店简介缓存（原 meta_cache.json）
    play_stats  游玩统计（原 config.json 里的 play_stats 字段）

所有写入都在事务内完成；旧 JSON 首次启动时幂等导入并保留为备份。
业务层仍通过 load_config/save_config 等原函数访问，签名保持不变。
"""
import json
import os
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
    path_key TEXT PRIMARY KEY,
    path     TEXT,
    folder   TEXT,
    appid    TEXT,
    score    REAL,
    tier     TEXT,
    data     TEXT NOT NULL,
    updated  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_games_appid  ON games(appid);
CREATE INDEX IF NOT EXISTS idx_games_folder ON games(folder);
CREATE TABLE IF NOT EXISTS game_meta (
    folder TEXT PRIMARY KEY,
    data   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steam_meta (
    appid TEXT PRIMARY KEY,
    data  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS play_stats (
    path TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
"""


def norm_key(path):
    """路径规范化主键：Windows 下大小写不敏感，且统一分隔符。"""
    return os.path.normcase(os.path.normpath(path or ""))


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(raw, default=None):
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Store:
    """线程安全的单库访问对象（HTTP 线程与在线识别线程会并发写入）。"""

    def __init__(self, db_path, meta_candidates=(), legacy=None, log=None):
        """legacy: dict(config=<path>, games=<path>, steam=<path>) —— 旧 JSON 位置。"""
        self.db_path = db_path
        self.meta_candidates = list(meta_candidates or ())
        self.legacy = legacy or {}
        self._log = log or (lambda msg: None)
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        # autocommit + 显式 BEGIN/COMMIT：避免 python sqlite3 的隐式事务与
        # 我们的事务边界冲突；不用 WAL，便携版目录里只留一个 gamebox.db。
        self._conn = sqlite3.connect(db_path, check_same_thread=False,
                                     timeout=15, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except Exception as e:
            self._log("store: pragma failed %r" % (e,))
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._migrate_legacy()
        # 评分库每次启动同步一次：外部文件覆盖 DB；文件缺失时保留 DB 副本。
        # 这正是「评分库漏打包 → 全库无分」的结构性兜底。
        self.sync_meta_files()

    # ---------- 基础 ----------
    def _tx(self):
        return self._conn

    def close(self):
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass

    # ---------- 旧 JSON 迁移（幂等，旧文件保留为备份） ----------
    def _migrate_legacy(self):
        with self._lock:
            c = self._conn
            # 持久标记：即使 reset_all() 清空了用户配置，重启也不会把旧 JSON
            # 备份重新导入（那会让「恢复初始设置」看起来没生效）。
            if c.execute("SELECT 1 FROM config WHERE key='__migrated__'").fetchone():
                return
            # 配置 + play_stats 拆表
            if not c.execute("SELECT 1 FROM config LIMIT 1").fetchone():
                p = self.legacy.get("config")
                if p and os.path.isfile(p):
                    try:
                        with open(p, encoding="utf-8") as f:
                            raw = json.load(f)
                        if isinstance(raw, dict):
                            for k, v in raw.items():
                                if k == "play_stats":
                                    continue
                                c.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)",
                                          (k, _dump(v)))
                            ps = raw.get("play_stats")
                            if isinstance(ps, dict):
                                for path, st in ps.items():
                                    c.execute("INSERT OR REPLACE INTO play_stats(path,data) VALUES(?,?)",
                                              (path, _dump(st)))
                            c.commit()
                            self._log("store: migrated config.json (%d keys)" % len(raw))
                    except Exception as e:
                        self._log("store: config migration failed %r" % (e,))
            # 游戏列表
            if not c.execute("SELECT 1 FROM games LIMIT 1").fetchone():
                p = self.legacy.get("games")
                if p and os.path.isfile(p):
                    try:
                        with open(p, encoding="utf-8") as f:
                            rows = json.load(f)
                        if isinstance(rows, list):
                            self._insert_games(rows, commit=False)
                            c.commit()
                            self._log("store: migrated games_cache.json (%d rows)" % len(rows))
                    except Exception as e:
                        self._log("store: games migration failed %r" % (e,))
            # Steam 简介缓存
            if not c.execute("SELECT 1 FROM steam_meta LIMIT 1").fetchone():
                p = self.legacy.get("steam")
                if p and os.path.isfile(p):
                    try:
                        with open(p, encoding="utf-8") as f:
                            cache = json.load(f)
                        if isinstance(cache, dict):
                            for k, v in cache.items():
                                c.execute("INSERT OR REPLACE INTO steam_meta(appid,data) VALUES(?,?)",
                                          (str(k), _dump(v)))
                            c.commit()
                            self._log("store: migrated meta_cache.json (%d keys)" % len(cache))
                    except Exception as e:
                        self._log("store: steam migration failed %r" % (e,))
            c.execute("INSERT OR REPLACE INTO config(key,value) VALUES('__migrated__','1')")
            c.commit()

    # ---------- 配置 ----------
    def get_config_raw(self):
        """返回 DB 中的配置 dict（含合并后的 play_stats），供 load_config 规范化。"""
        with self._lock:
            out = {}
            for r in self._conn.execute("SELECT key,value FROM config"):
                if r["key"].startswith("__"):     # 内部标记不暴露给业务层
                    continue
                out[r["key"]] = _load(r["value"])
            out["play_stats"] = {
                r["path"]: _load(r["data"], {})
                for r in self._conn.execute("SELECT path,data FROM play_stats")
            }
            return out

    def put_config_raw(self, cfg):
        """写入配置；play_stats 逐条 diff，避免每次全量重写整份大 JSON。"""
        with self._lock:
            c = self._conn
            try:
                c.execute("BEGIN")
                for k, v in cfg.items():
                    if k == "play_stats":
                        continue
                    c.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)",
                              (k, _dump(v)))
                ps = cfg.get("play_stats") or {}
                current = {r[0] for r in c.execute("SELECT path FROM play_stats")}
                for path, st in ps.items():
                    c.execute("INSERT OR REPLACE INTO play_stats(path,data) VALUES(?,?)",
                              (path, _dump(st)))
                for gone in current - set(ps):
                    c.execute("DELETE FROM play_stats WHERE path=?", (gone,))
                c.commit()
            except Exception as e:
                c.rollback()
                self._log("store: put_config failed %r" % (e,))

    # ---------- 游戏列表 ----------
    def _insert_games(self, items, commit=True):
        c = self._conn
        now = time.time()
        for it in items:
            key = norm_key(it.get("path"))
            if not key:
                continue
            c.execute(
                "INSERT OR REPLACE INTO games"
                "(path_key,path,folder,appid,score,tier,data,updated)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (key, it.get("path"), it.get("folder"),
                 str(it["appid"]) if it.get("appid") else None,
                 _to_num(it.get("score")), it.get("tier"),
                 _dump(it), now))
        if commit:
            c.commit()

    def load_games(self):
        with self._lock:
            return [_load(r["data"], {}) for r in
                    self._conn.execute("SELECT data FROM games")]

    def replace_games(self, items):
        """整表替换（扫描完成后调用），单事务保证不会出现半写状态。"""
        with self._lock:
            c = self._conn
            try:
                c.execute("BEGIN")
                c.execute("DELETE FROM games")
                self._insert_games(items, commit=False)
                c.commit()
            except Exception as e:
                c.rollback()
                self._log("store: replace_games failed %r" % (e,))

    def patch_games(self, patches):
        """只更新指定游戏的指定字段。patches: [(path, {field: value}), ...]

        在线识别 / 补全线程用它落盘，不再全量重写整张表，避免与扫描、
        收藏、游玩统计等写入互相覆盖。
        """
        if not patches:
            return
        with self._lock:
            c = self._conn
            try:
                c.execute("BEGIN")
                for path, patch in patches:
                    key = norm_key(path)
                    r = c.execute("SELECT data FROM games WHERE path_key=?", (key,)).fetchone()
                    if not r:
                        continue
                    it = _load(r["data"], {})
                    it.update(patch)
                    c.execute(
                        "UPDATE games SET path=?,folder=?,appid=?,score=?,tier=?,data=?,updated=?"
                        " WHERE path_key=?",
                        (it.get("path"), it.get("folder"),
                         str(it["appid"]) if it.get("appid") else None,
                         _to_num(it.get("score")), it.get("tier"),
                         _dump(it), time.time(), key))
                c.commit()
            except Exception as e:
                c.rollback()
                self._log("store: patch_games failed %r" % (e,))

    @staticmethod
    def merge_item(new, old):
        """合并一次扫描结果与库中已有记录。

        文件系统事实（path/folder/group/评分库字段）以 new 为准；
        在线识别成果（appid/中文名/idChecked）以 old 为准。
        这条规则集中在此，业务层不再各自手工合并。
        """
        m = dict(new)
        if not m.get("appid") and old.get("appid"):
            m["appid"] = old["appid"]
        recognized = bool(old.get("appid")) or old.get("note") == "已在线识别"
        if recognized:
            for k in ("cn", "en", "note", "known"):
                if old.get(k) is not None:
                    m[k] = old[k]
        for k in ("year", "score", "tier", "reviews", "plat"):
            cur = m.get(k)
            empty = cur in (None, 0, "", "暂无收录")
            ov = old.get(k)
            if empty and ov not in (None, 0, "", "暂无收录"):
                m[k] = ov
        if old.get("idChecked"):
            m["idChecked"] = True
        return m

    def merge_items(self, scanned, manual=()):
        """扫描结果 + 手动游戏与库中识别成果合并，返回最终列表（不落库）。"""
        with self._lock:
            old = {}
            for r in self._conn.execute("SELECT path_key,data FROM games"):
                old[r["path_key"]] = _load(r["data"], {})
        out = []
        for it in list(scanned) + list(manual):
            prev = old.get(norm_key(it.get("path")))
            out.append(self.merge_item(it, prev) if prev else dict(it))
        return out

    # ---------- 评分库（data/games.json） ----------
    def sync_meta_files(self):
        """把候选路径里的评分库同步进 DB；外部文件优先，缺文件时保留 DB 副本。"""
        applied = 0
        with self._lock:
            c = self._conn
            try:
                for p in self.meta_candidates:
                    if not os.path.isfile(p):
                        continue
                    try:
                        with open(p, encoding="utf-8") as f:
                            rows = json.load(f)
                    except Exception:
                        continue
                    if not isinstance(rows, list):
                        continue
                    c.execute("BEGIN")
                    for r in rows:
                        if isinstance(r, dict) and r.get("folder"):
                            c.execute("INSERT OR REPLACE INTO game_meta(folder,data) VALUES(?,?)",
                                      (r["folder"], _dump(r)))
                            applied += 1
                    c.commit()
            except Exception as e:
                try:
                    c.rollback()
                except Exception:
                    pass
                self._log("store: sync_meta_files failed %r" % (e,))
        if applied:
            self._log("store: synced %d metadata rows" % applied)
        return applied

    def load_meta(self):
        with self._lock:
            return {r["folder"]: _load(r["data"], {}) for r in
                    self._conn.execute("SELECT folder,data FROM game_meta")}

    # ---------- Steam 简介缓存 ----------
    def load_steam_meta(self):
        with self._lock:
            return {r["appid"]: _load(r["data"], {}) for r in
                    self._conn.execute("SELECT appid,data FROM steam_meta")}

    def save_steam_meta(self, cache):
        with self._lock:
            c = self._conn
            try:
                c.execute("BEGIN")
                for k, v in (cache or {}).items():
                    c.execute("INSERT OR REPLACE INTO steam_meta(appid,data) VALUES(?,?)",
                              (str(k), _dump(v)))
                c.commit()
            except Exception as e:
                c.rollback()
                self._log("store: save_steam_meta failed %r" % (e,))

    def clear_steam_meta(self):
        with self._lock:
            self._conn.execute("DELETE FROM steam_meta")
            self._conn.commit()

    # ---------- 维护 ----------
    def reset_all(self):
        """恢复初始设置：清空用户态数据，保留内置评分库与迁移标记。"""
        with self._lock:
            c = self._conn
            try:
                c.execute("BEGIN")
                for t in ("config", "games", "steam_meta", "play_stats"):
                    c.execute("DELETE FROM %s" % t)
                c.execute("INSERT OR REPLACE INTO config(key,value) VALUES('__migrated__','1')")
                c.commit()
            except Exception as e:
                c.rollback()
                self._log("store: reset_all failed %r" % (e,))
        self.sync_meta_files()
