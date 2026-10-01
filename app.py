"""温室分区轮灌编排台 —— Flask + SQLite 后端。"""

import json
import os
import sqlite3
from datetime import date, datetime, timedelta

from flask import Flask, Response, g, jsonify, request, send_from_directory

import scheduler
import water_quality as wq
import water_trace as wt
import disease_trace as dt
import crop_heat as ch
import drip_uniform as dri
import fertilizer_split as fz
import execution_check as ec

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "irrigation.db"))

app = Flask(__name__, static_folder="static", static_url_path="")


# ---------------------------------------------------------------- 数据库

def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH, timeout=5.0)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH, timeout=5.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS valves (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL UNIQUE,
            flow_lmin REAL NOT NULL,
            concur    INTEGER NOT NULL DEFAULT 1,
            source_id INTEGER REFERENCES water_sources(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS zones (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL UNIQUE,
            water_l       REAL NOT NULL,
            valve_id      INTEGER REFERENCES valves(id) ON DELETE SET NULL,
            earliest_slot INTEGER NOT NULL,
            latest_slot   INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS blackouts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            start_slot INTEGER NOT NULL,
            end_slot   INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schedule_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id    INTEGER NOT NULL UNIQUE REFERENCES zones(id) ON DELETE CASCADE,
            start_slot INTEGER NOT NULL,
            length     INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS water_sources (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT NOT NULL UNIQUE,
            note    TEXT NOT NULL DEFAULT '',
            created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS water_ranges (
            source_id INTEGER NOT NULL REFERENCES water_sources(id) ON DELETE CASCADE,
            metric    TEXT NOT NULL,
            low       REAL NOT NULL,
            high      REAL NOT NULL,
            PRIMARY KEY (source_id, metric)
        );
        CREATE TABLE IF NOT EXISTS water_samples (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES water_sources(id) ON DELETE CASCADE,
            ts        TEXT NOT NULL,
            ph        REAL NOT NULL,
            ec        REAL NOT NULL,
            turbidity REAL NOT NULL,
            nitrate   REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_water_samples_source_ts
            ON water_samples(source_id, ts);
        CREATE TABLE IF NOT EXISTS disease_tools (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            name   TEXT NOT NULL UNIQUE,
            note   TEXT NOT NULL DEFAULT '',
            retired INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS disease_cases (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            disease_name  TEXT NOT NULL,
            latency_hours REAL NOT NULL DEFAULT 24,
            status        TEXT NOT NULL DEFAULT 'open',
            note          TEXT NOT NULL DEFAULT '',
            created       TEXT NOT NULL,
            frozen_at     TEXT
        );
        CREATE TABLE IF NOT EXISTS disease_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id    INTEGER NOT NULL REFERENCES disease_cases(id) ON DELETE CASCADE,
            type       TEXT NOT NULL CHECK (type IN ('diagnosis','visit','disinfection')),
            ts         TEXT NOT NULL,
            tool_id    INTEGER,
            -- 工具名称冗余落库：冻结案件即使日后删除工具，台账也不丢失编号与名称。
            -- 因此 tool_id 不再建外键，开放案件的删除拦截在应用层保证。
            tool_name  TEXT,
            zone_id    INTEGER REFERENCES zones(id),
            qualified  INTEGER NOT NULL DEFAULT 0,
            note       TEXT NOT NULL DEFAULT '',
            created    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_disease_events_case_ts
            ON disease_events(case_id, ts, id);
        CREATE TABLE IF NOT EXISTS disease_snapshots (
            case_id     INTEGER PRIMARY KEY REFERENCES disease_cases(id) ON DELETE CASCADE,
            payload     TEXT NOT NULL,
            frozen_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS heat_batches (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id       INTEGER REFERENCES zones(id) ON DELETE RESTRICT,
            name          TEXT NOT NULL,
            crop          TEXT NOT NULL DEFAULT '',
            base_temp     REAL NOT NULL,
            cap_temp      REAL NOT NULL,
            target_gdd    REAL NOT NULL,
            max_gap_hours REAL NOT NULL,
            created       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS heat_readings (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id  INTEGER NOT NULL REFERENCES heat_batches(id) ON DELETE CASCADE,
            ts        TEXT NOT NULL,
            temp      REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_heat_readings_batch_ts
            ON heat_readings(batch_id, ts);
        CREATE INDEX IF NOT EXISTS idx_heat_batches_zone
            ON heat_batches(zone_id);
        CREATE TABLE IF NOT EXISTS drip_rounds (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            valve_id  INTEGER NOT NULL REFERENCES valves(id) ON DELETE RESTRICT,
            inspected TEXT NOT NULL,
            note      TEXT NOT NULL DEFAULT '',
            created   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_drip_rounds_inspected
            ON drip_rounds(inspected, id);
        CREATE TABLE IF NOT EXISTS drip_zones (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id             INTEGER NOT NULL REFERENCES drip_rounds(id) ON DELETE CASCADE,
            zone_id              INTEGER REFERENCES zones(id) ON DELETE SET NULL,
            -- 分区名称快照：巡检时归属阀门下的分区即使日后改名 / 删除，历史轮次台账不丢失
            zone_name            TEXT NOT NULL,
            inlet_pressure       REAL,
            base_inlet_pressure  REAL NOT NULL,
            design_flow          REAL NOT NULL,
            du_min               REAL NOT NULL,
            tail_drop_limit      REAL NOT NULL,
            low_flow_ratio       REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_drip_zones_round_zone
            ON drip_zones(round_id, zone_id);
        CREATE TABLE IF NOT EXISTS drip_samples (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            drip_zone_id INTEGER NOT NULL REFERENCES drip_zones(id) ON DELETE CASCADE,
            position    TEXT NOT NULL CHECK (position IN ('head','middle','tail')),
            label       TEXT NOT NULL,
            flow        REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_drip_samples_zone_pos_label
            ON drip_samples(drip_zone_id, position, label);
        CREATE TABLE IF NOT EXISTS fertilizer_recipes (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            zone_id   INTEGER REFERENCES zones(id) ON DELETE RESTRICT,
            cap_a     INTEGER NOT NULL,
            cap_b     INTEGER NOT NULL,
            note      TEXT NOT NULL DEFAULT '',
            created   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fertilizer_recipes_zone
            ON fertilizer_recipes(zone_id);
        CREATE TABLE IF NOT EXISTS fertilizer_items (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL REFERENCES fertilizer_recipes(id) ON DELETE CASCADE,
            name      TEXT NOT NULL,
            amount    INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fertilizer_items_recipe_name
            ON fertilizer_items(recipe_id, name);
        CREATE TABLE IF NOT EXISTS fertilizer_pairs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL REFERENCES fertilizer_recipes(id) ON DELETE CASCADE,
            -- 落库时统一为 a_id < b_id，唯一索引即可防止重复不相容关系
            a_id      INTEGER NOT NULL REFERENCES fertilizer_items(id) ON DELETE CASCADE,
            b_id      INTEGER NOT NULL REFERENCES fertilizer_items(id) ON DELETE CASCADE,
            CHECK (a_id < b_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fertilizer_pairs_pair
            ON fertilizer_pairs(recipe_id, a_id, b_id);
        CREATE TABLE IF NOT EXISTS verify_batches (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            plan_date     TEXT NOT NULL,
            note          TEXT NOT NULL DEFAULT '',
            plan_revision INTEGER,
            created       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_verify_batches_date
            ON verify_batches(plan_date, id);
        CREATE TABLE IF NOT EXISTS verify_events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id  INTEGER NOT NULL REFERENCES verify_batches(id) ON DELETE CASCADE,
            event_id  TEXT NOT NULL,
            valve_id  INTEGER NOT NULL REFERENCES valves(id) ON DELETE RESTRICT,
            start_min INTEGER NOT NULL,
            end_min   INTEGER NOT NULL,
            flow_lmin REAL NOT NULL,
            created   TEXT NOT NULL,
            UNIQUE (batch_id, event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_verify_events_batch
            ON verify_events(batch_id, start_min, event_id);
        """
    )
    _migrate_db(db)
    cur = db.execute("SELECT value FROM settings WHERE key='plan_date'")
    if cur.fetchone() is None:
        db.execute("INSERT INTO settings(key,value) VALUES('plan_date',?)",
                   (date.today().isoformat(),))
        db.execute("INSERT INTO settings(key,value) VALUES('total_flow','100')")
        # plan_revision 已由 v3 迁移插入（新库迁移同样执行），这里幂等兜底
        db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('plan_revision','1')")
        db.commit()
        _seed_sample(db)
    if db.execute("SELECT COUNT(*) FROM water_sources").fetchone()[0] == 0:
        _seed_water(db)
    _bind_sample_valve_sources(db)
    if db.execute("SELECT COUNT(*) FROM disease_tools").fetchone()[0] == 0:
        _seed_disease(db)
    if db.execute("SELECT COUNT(*) FROM heat_batches").fetchone()[0] == 0:
        _seed_heat(db)
    if db.execute("SELECT COUNT(*) FROM drip_rounds").fetchone()[0] == 0:
        _seed_drip(db)
    if db.execute("SELECT COUNT(*) FROM fertilizer_recipes").fetchone()[0] == 0:
        _seed_fertilizer(db)
    if db.execute("SELECT COUNT(*) FROM verify_batches").fetchone()[0] == 0:
        _seed_verify(db)
    db.close()


def _migrate_db(db):
    """版本化迁移。

    v1：为 disease_events 增加冗余 tool_name 列。
    旧表 tool_id 带外键，删除工具时只能把历史事件的 tool_id 置空，
    导致冻结案件台账丢失工具编号与名称。新表去掉该外键（删除拦截改在
    应用层，只拦开放案件），并把工具名称随事件落库，重启后仍可查询。

    v2：用水回溯——为 valves 增加 source_id，每个阀门绑定一个水源。

    v3：轮灌计划防覆盖修订——settings.plan_revision 作为计划及其约束的
    乐观并发版本：仅在客户端基线等于当前修订时原子写计划并递增；
    阀门流量 / 并发、分区约束、停水时段、总流量任一变更也递增，
    使在旧约束上打开的页面基线一并失效。

    v4：执行核验——新增 verify_batches / verify_events 两张表
    （CREATE TABLE IF NOT EXISTS 已在建库脚本中幂等创建，
    这里仅把旧库版本号推进到 4）。
    """
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        cols = [r["name"] for r in db.execute("PRAGMA table_info(disease_events)")]
        if "tool_name" not in cols:
            db.executescript(
                """
                CREATE TABLE disease_events_new (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id    INTEGER NOT NULL REFERENCES disease_cases(id) ON DELETE CASCADE,
                    type       TEXT NOT NULL CHECK (type IN ('diagnosis','visit','disinfection')),
                    ts         TEXT NOT NULL,
                    tool_id    INTEGER,
                    tool_name  TEXT,
                    zone_id    INTEGER REFERENCES zones(id),
                    qualified  INTEGER NOT NULL DEFAULT 0,
                    note       TEXT NOT NULL DEFAULT '',
                    created    TEXT NOT NULL
                );
                INSERT INTO disease_events_new
                    (id, case_id, type, ts, tool_id, tool_name, zone_id, qualified, note, created)
                SELECT e.id, e.case_id, e.type, e.ts, e.tool_id, t.name,
                       e.zone_id, e.qualified, e.note, e.created
                FROM disease_events e
                LEFT JOIN disease_tools t ON t.id = e.tool_id;
                DROP TABLE disease_events;
                ALTER TABLE disease_events_new RENAME TO disease_events;
                CREATE INDEX idx_disease_events_case_ts
                    ON disease_events(case_id, ts, id);
                """
            )
            # 旧版本删除工具时已被置空 tool_id 的冻结案件事件，尽力从冻结快照的
            # 事件链里还原（编号 + 名称）；不在任何最短链上的事件无法结构化还原。
            for snap in db.execute("SELECT case_id, payload FROM disease_snapshots"):
                try:
                    payload = json.loads(snap["payload"])
                except (TypeError, ValueError):
                    continue
                by_event = {}
                for block in (payload.get("zones") or []) + (payload.get("tools") or []):
                    for c in block.get("chain") or []:
                        if c.get("tool_id") is not None:
                            by_event[c["id"]] = (c["tool_id"], c.get("tool_name"))
                for eid, (tid, tname) in by_event.items():
                    db.execute(
                        "UPDATE disease_events SET tool_id = COALESCE(tool_id, ?),"
                        " tool_name = COALESCE(tool_name, ?)"
                        " WHERE id = ? AND tool_name IS NULL",
                        (tid, tname, eid))
            db.commit()
        db.execute("PRAGMA user_version = 1")
        db.commit()

    if db.execute("PRAGMA user_version").fetchone()[0] >= 3:
        return
    # 用水回溯：每个阀门绑定一个水源（valves.source_id）。
    # 旧库补列；删除水源时绑定自动置空（ON DELETE SET NULL 随新列定义生效，
    # 旧版 ALTER 新增列上的 FK 动作受支持），回溯结果改为证据不足。
    if db.execute("PRAGMA user_version").fetchone()[0] < 2:
        cols = [r["name"] for r in db.execute("PRAGMA table_info(valves)")]
        if "source_id" not in cols:
            db.execute(
                "ALTER TABLE valves ADD COLUMN source_id INTEGER"
                " REFERENCES water_sources(id) ON DELETE SET NULL")
        db.execute("PRAGMA user_version = 2")
        db.commit()

    # v3：旧库补出 plan_revision——已有计划的库从修订 1 开始，
    # 各页面加载后即持有基线；新库的 settings 种子在 init_db 中插入。
    db.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES('plan_revision','1')")
    db.execute("PRAGMA user_version = 3")
    db.commit()

    # v4：执行核验两张表由建库脚本 IF NOT EXISTS 幂等创建，仅推进版本号。
    db.execute("PRAGMA user_version = 4")
    db.commit()


# ---------------------------------------------------------------- 示例数据

SAMPLE = {
    "total_flow": 60,
    "valves": [
        ("北侧干管", 25, 1),
        ("中部干管", 25, 1),
        ("苗床支管", 10, 1),
    ],
    "blackouts": [
        ("泵房例行检修", "07:30", "08:00"),
    ],
    # 名称, 水量(L), 阀门序号(0基), 最早, 最晚
    "zones": [
        ("番茄区", 3000, 0, "06:00", "10:00"),
        ("黄瓜区", 2250, 1, "18:00", "22:00"),
        ("草莓区", 1500, 1, "05:30", "08:30"),
        ("叶菜区", 750, 1, "08:00", "11:00"),
        ("育苗区", 450, 2, "07:00", "09:00"),
    ],
}


def _seed_sample(db):
    db.execute("UPDATE settings SET value=? WHERE key='total_flow'",
               (str(SAMPLE["total_flow"]),))
    valve_ids = []
    for name, flow, concur in SAMPLE["valves"]:
        cur = db.execute("INSERT INTO valves(name,flow_lmin,concur) VALUES(?,?,?)",
                         (name, flow, concur))
        valve_ids.append(cur.lastrowid)
    for name, water_l, vi, earliest, latest in SAMPLE["zones"]:
        db.execute(
            "INSERT INTO zones(name,water_l,valve_id,earliest_slot,latest_slot)"
            " VALUES(?,?,?,?,?)",
            (name, water_l, valve_ids[vi],
             scheduler.hhmm_to_slot(earliest), scheduler.hhmm_to_slot(latest)))
    for name, start, end in SAMPLE["blackouts"]:
        db.execute("INSERT INTO blackouts(name,start_slot,end_slot) VALUES(?,?,?)",
                   (name, scheduler.hhmm_to_slot(start), scheduler.hhmm_to_slot(end)))

    # 内置一份当日轮灌计划，使「用水回溯」开箱即可演示：
    # 贪心编排结果（15 分钟槽，总流量 60 L/min，07:30–08:00 停水）：
    #   育苗区 07:00–07:45、番茄区 08:00–10:00、草莓区 05:30–08:00、
    #   黄瓜区 18:00–19:30、叶菜区 10:00–10:30。
    db.commit()
    _seed_schedule(db)


def _bind_sample_valve_sources(db):
    """把示例阀门绑到示例水源（北侧/中部干管 -> 机井水；苗床支管不绑）。

    在水源与阀门均就绪后调用；只设置仍为 NULL 的绑定，避免覆盖用户改动。
    """
    well = db.execute(
        "SELECT id FROM water_sources WHERE name=?", ("1# 机井水",)).fetchone()
    if well is None:
        return
    for name in ("北侧干管", "中部干管"):
        db.execute(
            "UPDATE valves SET source_id=? WHERE name=? AND source_id IS NULL",
            (well["id"], name))


def _seed_schedule(db):
    """用当前示例对象贪心编排并写入 schedule_items（供用水回溯演示）。"""
    valves = [valve_dict(r) for r in db.execute("SELECT * FROM valves ORDER BY id")]
    zones = [zone_dict(r, {v["id"]: v for v in valves})
             for r in db.execute("SELECT * FROM zones ORDER BY id")]
    blackouts = [blackout_dict(r)
                 for r in db.execute("SELECT * FROM blackouts ORDER BY id")]
    result = scheduler.generate(zones, valves, blackouts, SAMPLE["total_flow"])
    for it in result["items"]:
        db.execute(
            "INSERT INTO schedule_items(zone_id,start_slot,length) VALUES(?,?,?)",
            (it["zone_id"], it["start_slot"], it["length"]))


# ---------------------------------------------------------------- 水质示例数据

def _seed_water(db):
    """两个示例水源：机井水质硝酸盐连续超标；集雨水池水浊度处于观察。"""
    now = datetime.now().replace(microsecond=0)

    def iso(days_ago, hhmm):
        h, m = map(int, hhmm.split(":"))
        t = (now - timedelta(days=days_ago)).replace(hour=h, minute=m, second=0)
        return t.isoformat()

    seeds = [
        ("1# 机井水", "泵房北侧深水井", [
            # days_ago, 时间, pH,  EC,   浊度, 硝酸盐
            (2, "08:00", 7.2, 1.10, 2.1, 18.0),
            (2, "14:00", 7.3, 1.15, 2.4, 22.0),
            (1, "08:00", 7.1, 1.20, 2.6, 34.0),  # 硝酸盐首次越界 -> 观察
            (1, "14:00", 7.0, 1.25, 2.8, 36.5),  # 连续两次 -> 超标
            (0, "08:00", 7.1, 1.18, 2.5, 35.0),
            (0, "09:00", 7.2, 1.16, 2.3, 33.0),  # 仍超标
        ]),
        ("雨水收集池", "温室南侧集雨沉淀池", [
            (1, "07:30", 7.8, 0.40, 5.0, 6.0),
            (1, "13:30", 7.9, 0.45, 12.0, 7.5),  # 浊度首次越界 -> 观察
            (0, "07:30", 7.7, 0.42, 8.0, 6.5),   # 回到区间，仅 1 次 -> 观察
        ]),
    ]
    for name, note, rows in seeds:
        cur = db.execute(
            "INSERT INTO water_sources(name,note,created) VALUES(?,?,?)",
            (name, note, now.isoformat()))
        sid = cur.lastrowid
        for key in wq.METRIC_KEYS:
            lo, hi = wq.METRICS[key][2], wq.METRICS[key][3]
            db.execute(
                "INSERT INTO water_ranges(source_id,metric,low,high) VALUES(?,?,?,?)",
                (sid, key, lo, hi))
        for days_ago, hhmm, ph, ec, turb, nitrate in rows:
            db.execute(
                "INSERT INTO water_samples(source_id,ts,ph,ec,turbidity,nitrate)"
                " VALUES(?,?,?,?,?,?)",
                (sid, iso(days_ago, hhmm), ph, ec, turb, nitrate))
    db.commit()


# ---------------------------------------------------------------- 病害追踪示例数据

def _seed_disease(db):
    """一套病害接触追踪示例：灰霉病确诊 + 可复用工具的到访/消毒时间线。

    潜伏时长 24 小时；覆盖「潜伏分区暂不传染」「合格消毒截断」
    「不合格消毒不截断」三类关键规则。
    """
    now = datetime.now().replace(microsecond=0)
    base_day = datetime.combine((now - timedelta(days=2)).date(), datetime.min.time())

    def stamp(d_off, hhmm):
        return (base_day + timedelta(days=d_off)).strftime("%Y-%m-%d") + f"T{hhmm}:00"

    tool_names = ["修枝剪 A", "采收筐 1 号", "周转托盘", "剪刀 B（备用）"]
    tool_ids = []
    for name in tool_names:
        cur = db.execute("INSERT INTO disease_tools(name,note,retired) VALUES(?,?,0)",
                         (name, ""))
        tool_ids.append(cur.lastrowid)

    zone_by_name = {r["name"]: r["id"] for r in db.execute("SELECT id,name FROM zones")}
    needed = ["番茄区", "黄瓜区", "叶菜区", "育苗区"]
    if not all(n in zone_by_name for n in needed):
        db.commit()
        return  # 分区被清空/改名时不强行演示，但保留工具登记

    tq, tool_s1, tool_t1 = tool_ids[:3]
    z_tomato = zone_by_name["番茄区"]
    z_cucumber = zone_by_name["黄瓜区"]
    z_leaf = zone_by_name["叶菜区"]
    z_seedling = zone_by_name["育苗区"]

    # 相对第 0 天的小时偏移 -> 当日时刻
    rows = [
        # type, day_offset, 时刻, tool, zone, qualified, note
        ("diagnosis",   0, "09:00", None,   z_tomato,   0, "番茄区灰霉病确诊，源头分区"),
        ("visit",       0, "10:00", tq,     z_tomato,   0, "修枝剪进入确诊分区，被污染"),
        ("visit",       0, "11:00", tq,     z_cucumber, 0, "到访黄瓜区，立即暴露"),
        ("visit",       0, "12:00", tool_s1, z_cucumber, 0, "黄瓜区仍在潜伏期，采收筐未污染"),
        ("disinfection", 0, "12:30", tq,    None,       0, "仅清水擦拭，消毒不合格"),
        ("visit",       0, "13:00", tq,     z_leaf,     0, "消毒不达标，继续传播至叶菜区"),
        ("disinfection", 0, "16:00", tq,    None,       1, "按规程喷雾消毒，合格"),
        ("visit",       0, "17:00", tq,     z_seedling, 0, "已合格消毒，育苗区不受污染"),
        ("visit",       1, "11:30", tool_s1, z_cucumber, 0, "黄瓜区潜伏期满具传染性，采收筐被污染"),
        ("visit",       1, "12:30", tool_s1, z_seedling, 0, "采收筐到访育苗区，立即暴露"),
        ("disinfection", 1, "13:00", tool_s1, None,      1, "采收筐合格消毒，截断传播"),
        ("visit",       1, "15:00", tool_t1, z_leaf,     0, "周转托盘进入叶菜区，被污染"),
        ("visit",       1, "16:00", tool_t1, z_seedling, 0, "育苗区已有更早暴露，循环只保留最早"),
        ("disinfection", 1, "17:00", tool_t1, None,      1, "周转托盘合格消毒"),
    ]

    cur = db.execute(
        "INSERT INTO disease_cases(disease_name,latency_hours,status,note,created)"
        " VALUES(?,24,'open',?,?)",
        ("灰霉病", "示例案件：工具跨分区接触追踪", now.isoformat()))
    case_id = cur.lastrowid
    for etype, d_off, hhmm, tool, zone, qualified, note in rows:
        tool_name = next((n for n, tid in zip(tool_names, tool_ids) if tid == tool), None)
        db.execute(
            "INSERT INTO disease_events(case_id,type,ts,tool_id,tool_name,zone_id,"
            "qualified,note,created) VALUES(?,?,?,?,?,?,?,?,?)",
            (case_id, etype, stamp(d_off, hhmm), tool, tool_name, zone, qualified, note,
             now.isoformat()))
    db.commit()


# ---------------------------------------------------------------- 作物积温示例数据

def _seed_heat(db):
    """两个积温批次示例，覆盖「已可外推」与「样本不足 / 缺口不可预测」两类情形。

    批次一（番茄区 · 越冬番茄）：每天 08:00 / 20:00 两条读数、允许间隔 13h，
    共 10 天，含 7 个无缺口完整日，尚差一些积温 -> 可用 7 个完整日均值外推。
    批次二（黄瓜区）：读数稀疏且含一个超过 12h 允许间隔的缺口，
    完整日不足 7 个 -> 明确不可预测，并在缺口清单登记。
    """
    now = datetime.now().replace(microsecond=0)
    zone_by_name = {r["name"]: r["id"] for r in db.execute("SELECT id,name FROM zones")}
    if "番茄区" not in zone_by_name:
        return

    def stamp(d_off, hhmm):
        return ((now - timedelta(days=9 - d_off))
                .replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0)
                .isoformat())

    cur = db.execute(
        "INSERT INTO heat_batches(zone_id,name,crop,base_temp,cap_temp,"
        "target_gdd,max_gap_hours,created) VALUES(?,?,?,?,?,?,?,?)",
        (zone_by_name["番茄区"], "越冬番茄一批", "番茄",
         10.0, 30.0, 150.0, 13.0, now.isoformat()))
    bid = cur.lastrowid
    # 每天两条：08:00 较凉、20:00 白天积温后的较高温度（含越限截断演示）
    temps = [
        (8, 22), (9, 24), (7, 21), (10, 26), (11, 28),
        (9, 23), (8, 22), (10, 32), (9, 25), (8, 23),
    ]
    for d, (m, e) in enumerate(temps):
        db.execute("INSERT INTO heat_readings(batch_id,ts,temp) VALUES(?,?,?)",
                   (bid, stamp(d, "08:00"), m))
        db.execute("INSERT INTO heat_readings(batch_id,ts,temp) VALUES(?,?,?)",
                   (bid, stamp(d, "20:00"), e))

    if "黄瓜区" in zone_by_name:
        cur = db.execute(
            "INSERT INTO heat_batches(zone_id,name,crop,base_temp,cap_temp,"
            "target_gdd,max_gap_hours,created) VALUES(?,?,?,?,?,?,?,?)",
            (zone_by_name["黄瓜区"], "秋黄瓜补录批", "黄瓜",
             12.0, 32.0, 200.0, 12.0, now.isoformat()))
        bid2 = cur.lastrowid
        # 只录了最近 4 天，09-24 20:00 -> 09-25 14:00 漏了一次夜班（18h > 12h 允许间隔），
        # 且完整日不足 7 个 -> 不可预测并登记缺口
        rows = [
            (3, "08:00", 14), (3, "20:00", 24),
            (2, "08:00", 15), (2, "20:00", 26),
            (1, "08:00", 16),
            (0, "14:00", 27), (0, "20:00", 25),
        ]
        for d_off, hhmm, temp in rows:
            t = (now - timedelta(days=d_off)).replace(
                hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0)
            db.execute("INSERT INTO heat_readings(batch_id,ts,temp) VALUES(?,?,?)",
                       (bid2, t.isoformat(), temp))
    db.commit()


# ---------------------------------------------------------------- 滴灌巡检示例数据

def _seed_drip(db):
    """两轮滴灌均匀度巡检示例，覆盖三类关键判定。

    一轮（中部干管 · 秋茬滴灌首轮巡检）：
      黄瓜区 = 支管压损（压力合格、DU 不合格、尾首降幅达阈值）；
      草莓区 = 合格但 1 个采样点低于堵塞阈值（疑似滴头堵塞）；
      叶菜区 = 入口压力偏低但 DU 合格（共享供压候选，仅 1/3 不触发）。
    二轮（中部干管 · 供压异常复核巡检）：
      3/3 分区入口压力低于基准且 DU 合格 -> 整轮共享供压异常。
    """
    now = datetime.now().replace(microsecond=0)
    valve = db.execute("SELECT id FROM valves WHERE name=?", ("中部干管",)).fetchone()
    zone_rows = db.execute(
        "SELECT id,name FROM zones WHERE name IN ('黄瓜区','草莓区','叶菜区')").fetchall()
    if valve is None or len(zone_rows) < 3:
        return  # 示例阀门 / 分区被清空时不强行演示
    valve_id = valve["id"]
    zone_by_name = {r["name"]: r["id"] for r in zone_rows}

    # 基准入口压力 0.10 MPa、设计滴头流量 2.0 L/h、DU 下限 0.80、
    # 尾首降幅阈值 10%、低流量比例 0.70（堵塞阈值 1.4 L/h）
    cfg = (0.10, 2.0, 0.80, 0.10, 0.70)

    def make_samples(spec):
        """spec: {position: [flow, ...]} -> [(position, label, flow)]。

        各段按首/中/尾各自从 1 号开始编号，与田间编号习惯一致。
        """
        out = []
        prefix = {"head": "H", "middle": "M", "tail": "T"}
        for pos, flows in spec.items():
            for i, f in enumerate(flows, 1):
                out.append((pos, f"{prefix[pos]}{i:02d}", f))
        return out

    rounds = [
        ("秋茬滴灌首轮巡检", 2, [
            # (分区名, 实测入口压力, 样本)
            ("黄瓜区", 0.12, {
                # 首段接近设计流量，向尾段明显衰减 -> DU≈0.687、尾首降幅 42.2%
                "head": [1.95, 2.00, 2.05, 1.90, 2.00, 1.98, 2.02, 1.96],
                "middle": [1.70, 1.55, 1.60, 1.75, 1.50],
                "tail": [1.20, 1.05, 1.15, 1.30, 0.95],
            }),
            ("草莓区", 0.11, {
                # 整体均匀，仅 H01 段一个 1.1 L/h 滴头 < 堵塞阈值 1.4
                "head": [2.00, 2.00, 2.00, 2.00, 2.00, 2.00, 2.00, 1.10],
                "middle": [2.00, 2.00, 2.00, 2.00],
                "tail": [1.95, 1.95, 1.95],
            }),
            ("叶菜区", 0.08, {
                # 压力偏低但均匀度仍合格
                "head": [2.00, 2.00, 2.00],
                "middle": [1.95, 2.00, 1.90],
                "tail": [1.90, 1.95, 2.00, 1.95],
            }),
        ]),
        ("供压异常复核巡检", 0, [
            # 三个分区入口压力全部低于基准、DU 全部合格 -> 2*3>3 共享供压异常
            ("黄瓜区", 0.07, {
                "head": [1.70, 1.65, 1.72, 1.68, 1.70, 1.66, 1.74, 1.69],
                "middle": [1.65, 1.70, 1.62, 1.68, 1.66],
                "tail": [1.60, 1.64, 1.58, 1.62, 1.66],
            }),
            ("草莓区", 0.07, {
                "head": [1.70, 1.68, 1.72, 1.66, 1.70, 1.69],
                "middle": [1.65, 1.68, 1.63, 1.66],
                "tail": [1.62, 1.65, 1.60, 1.64],
            }),
            ("叶菜区", 0.06, {
                "head": [1.60, 1.58, 1.62],
                "middle": [1.55, 1.60, 1.57],
                "tail": [1.52, 1.56, 1.54, 1.58],
            }),
        ]),
    ]

    for rname, days_ago, zones in rounds:
        inspected = (now - timedelta(days=days_ago)).replace(
            hour=9, minute=0, second=0).isoformat()
        rc = db.execute(
            "INSERT INTO drip_rounds(name,valve_id,inspected,note,created)"
            " VALUES(?,?,?,?,?)",
            (rname, valve_id, inspected, "内置示例巡检", now.isoformat()))
        rid = rc.lastrowid
        for zname, inlet, spec in zones:
            zc = db.execute(
                "INSERT INTO drip_zones(round_id,zone_id,zone_name,inlet_pressure,"
                "base_inlet_pressure,design_flow,du_min,tail_drop_limit,low_flow_ratio)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, zone_by_name[zname], zname, inlet, *cfg))
            dzid = zc.lastrowid
            for pos, label, flow in make_samples(spec):
                db.execute(
                    "INSERT INTO drip_samples(drip_zone_id,position,label,flow)"
                    " VALUES(?,?,?,?)", (dzid, pos, label, flow))
    db.commit()


# ---------------------------------------------------------------- 肥料母液分罐示例数据

def _seed_fertilizer(db):
    """一份母液分罐示例配方（挂在番茄区）。

    不相容图为一条链（硝酸钙-硝酸钾-硫酸镁-磷酸二氢钾）加孤立点微量元素，
    A/B 罐各 2000 mL；总母液 3000 mL，最优组合恰好两罐各 1500 mL（体积差 0），
    A 罐为硝酸钙 + 硫酸镁，B 罐为硝酸钾 + 磷酸二氢钾 + 微量元素。
    """
    now = datetime.now().replace(microsecond=0).isoformat()
    zone = db.execute("SELECT id FROM zones WHERE name=?", ("番茄区",)).fetchone()
    zone_id = zone["id"] if zone else None
    rc = db.execute(
        "INSERT INTO fertilizer_recipes(name,zone_id,cap_a,cap_b,note,created)"
        " VALUES(?,?,?,?,?,?)",
        ("越冬番茄母液配方", zone_id, 2000, 2000, "内置示例配方", now))
    rid = rc.lastrowid
    # 名称, 用量 mL
    items = [
        ("硝酸钙", 800),
        ("硝酸钾", 600),
        ("硫酸镁", 700),
        ("磷酸二氢钾", 500),
        ("微量元素", 400),
    ]
    ids = {}
    for name, amount in items:
        cur = db.execute(
            "INSERT INTO fertilizer_items(recipe_id,name,amount) VALUES(?,?,?)",
            (rid, name, amount))
        ids[name] = cur.lastrowid
    # 钙盐与磷酸盐 / 硫酸盐不相容等（示例链）
    for a, b in [("硝酸钙", "硝酸钾"), ("硝酸钾", "硫酸镁"),
                 ("硫酸镁", "磷酸二氢钾")]:
        x, y = sorted((ids[a], ids[b]))
        db.execute(
            "INSERT INTO fertilizer_pairs(recipe_id,a_id,b_id) VALUES(?,?,?)",
            (rid, x, y))
    db.commit()


# ---------------------------------------------------------------- 执行核验示例数据

def _seed_verify(db):
    """一份执行核验示例批次，覆盖提前 / 中断缺水 / 计划外 / 阀门并发 /
    总流量冲突五类情形。

    事件按内置示例计划（_seed_schedule 的贪心结果）动态生成，因此计划重排后
    仍可对齐；计划日期取当前 plan_date 设置。
    """
    settings = {r["key"]: r["value"]
                for r in db.execute("SELECT key,value FROM settings")}
    day_raw = settings.get("plan_date", date.today().isoformat())
    try:
        day = ec.parse_day(day_raw)
    except ValueError:
        return
    valve_by_name = {r["name"]: r for r in db.execute("SELECT * FROM valves")}
    if not db.execute("SELECT 1 FROM schedule_items LIMIT 1").fetchone():
        return

    def slot_dt(slot, delta_min=0):
        ts = datetime.combine(day, datetime.min.time()) + timedelta(
            minutes=slot * scheduler.SLOT_MIN + delta_min)
        return ts.replace(second=0, microsecond=0).isoformat()

    # 实际贪心计划（scheduler.generate）：
    #   草莓 05:30–06:30、番茄 08:00–10:00、叶菜 08:00–08:30、
    #   育苗 08:00–08:45、黄瓜 18:00–19:30
    zid_by_name = {r["name"]: r["id"]
                   for r in db.execute("SELECT id,name FROM zones")}
    by_zone = {r["zone_id"]: (r["start_slot"], r["start_slot"] + r["length"])
               for r in db.execute(
                   "SELECT zone_id,start_slot,length FROM schedule_items")}
    rows = []

    def add_for_zone(zone_name, valve_name, eid, start_delta=0, end_delta=0,
                     flow=None):
        zid = zid_by_name.get(zone_name)
        if zid is None or zid not in by_zone:
            return
        v = valve_by_name.get(valve_name)
        if v is None:
            return
        s, e = by_zone[zid]
        rows.append((
            eid, v["id"], slot_dt(s, start_delta), slot_dt(e, end_delta),
            flow if flow is not None else v["flow_lmin"]))

    # 草莓区：提前 90 分钟开、按时停（计划外提前 04:00–05:30，计划内足额）
    add_for_zone("草莓区", "中部干管", "SV-STRAWBERRY-01", start_delta=-90)
    # 番茄区：提前 10 分钟停（中断 10 分钟，缺水 250 L）
    add_for_zone("番茄区", "北侧干管", "SV-TOMATO-01", end_delta=-10)
    # 黄瓜区：提前 10 分钟开（计划外 250 L，计划内足额）
    add_for_zone("黄瓜区", "中部干管", "SV-CUCUMBER-01", start_delta=-10)
    # 叶菜区：08:00–08:16 运行 25 L/min；08:10–08:16 演示事件 40 L/min
    # 并发（并集按 40 计），提前 14 分钟停，缺水 150 L（需补 1 槽）
    add_for_zone("叶菜区", "中部干管", "SV-LEAF-01", end_delta=-14)
    # 育苗区：完整执行 + 一条并发事件（触发苗床支管并发超限）
    add_for_zone("育苗区", "苗床支管", "SV-SEEDLING-01")
    yz = by_zone.get(zid_by_name.get("育苗区"))
    yv = valve_by_name.get("苗床支管")
    if yz and yv:
        rows.append((
            "SV-SEEDLING-02", yv["id"],
            slot_dt(yz[0], 20), slot_dt(yz[0], 40), yv["flow_lmin"]))
    # 总流量冲突演示：08:10–08:20 中部干管以 40 L/min 运行。
    # 08:10–08:16 与叶菜计划相交而归属叶菜（同阀门两路并发同时超限），
    # 叠加番茄 25 + 实际 40 + 育苗 10 = 75 > 60 -> 总流量超限；
    # 08:16–08:20 叶菜事件已结束，该子段不压任何中部干管计划 -> 计划外越界，
    # 叠加番茄 25 + 40 + 育苗 10 = 75 > 60 仍超限（完整演示切分与两类判定）。
    tv = valve_by_name.get("中部干管")
    if tv:
        rows.append((
            "SV-DEMO-FLOW", tv["id"], slot_dt(32, 10), slot_dt(32, 20), 40.0))

    if not rows:
        return
    rows.sort(key=lambda r: r[2])
    now = datetime.now().replace(microsecond=0).isoformat()
    revision = int(settings.get("plan_revision", 1))
    cur = db.execute(
        "INSERT INTO verify_batches(name,plan_date,note,plan_revision,created)"
        " VALUES(?,?,?,?,?)",
        ("示例核验批次", day_raw,
         "内置示例：提前 / 中断缺水 / 计划外 / 阀门并发 / 总流量冲突",
         revision, now))
    bid = cur.lastrowid
    for eid, vid, start, end, flow in rows:
        db.execute(
            "INSERT INTO verify_events(batch_id,event_id,valve_id,start_min,"
            "end_min,flow_lmin,created) VALUES(?,?,?,?,?,?,?)",
            (bid, eid, vid,
             int((datetime.fromisoformat(start)
                  - datetime.combine(day, datetime.min.time())).total_seconds()
                 // 60),
             int((datetime.fromisoformat(end)
                  - datetime.combine(day, datetime.min.time())).total_seconds()
                 // 60),
             flow, now))
    db.commit()


# ---------------------------------------------------------------- 序列化

def valve_dict(row):
    return {"id": row["id"], "name": row["name"],
            "flow_lmin": row["flow_lmin"], "concur": row["concur"],
            "source_id": row["source_id"] if "source_id" in row.keys() else None}


def zone_dict(row, valve_by_id):
    v = valve_by_id.get(row["valve_id"])
    return {
        "id": row["id"],
        "name": row["name"],
        "water_l": row["water_l"],
        "valve_id": row["valve_id"],
        "valve_name": v["name"] if v else "（未绑定）",
        "flow_lmin": v["flow_lmin"] if v else 0,
        "earliest_slot": row["earliest_slot"],
        "latest_slot": row["latest_slot"],
        "earliest": scheduler.slot_to_hhmm(row["earliest_slot"]),
        "latest": scheduler.slot_to_hhmm(row["latest_slot"]),
    }


def blackout_dict(row):
    return {"id": row["id"], "name": row["name"],
            "start_slot": row["start_slot"], "end_slot": row["end_slot"],
            "start": scheduler.slot_to_hhmm(row["start_slot"]),
            "end": scheduler.slot_to_hhmm(row["end_slot"])}


def item_dict(row):
    return {"id": row["id"], "zone_id": row["zone_id"],
            "start_slot": row["start_slot"], "length": row["length"]}


# ---------------------------------------------------------------- 计划修订（乐观并发）

class RevisionConflict(Exception):
    """客户端基线修订已过期：计划或其约束在其加载后被其他窗口修改。"""

    def __init__(self, current_revision):
        super().__init__("计划修订已过期，请按差异复核后再提交")
        self.current_revision = current_revision


def get_plan_revision(db):
    row = db.execute(
        "SELECT value FROM settings WHERE key='plan_revision'").fetchone()
    return int(row["value"]) if row is not None else 1


def bump_plan_revision(db):
    """递增计划修订。

    必须与对应业务写入处于同一事务（先于 commit 调用）：
    阀门流量 / 并发、分区约束、停水时段、总流量或计划本身变更，
    都会令所有持有旧基线的页面提交时被 CAS 拒绝。
    """
    db.execute(
        "UPDATE settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
        " WHERE key='plan_revision'")


def commit_schedule(db, clean, base_revision):
    """在单个 IMMEDIATE 事务里做「校验基线 → 整表替换 → 递增修订」。

    基线不匹配时回滚并抛 RevisionConflict，不留下任何部分更新；
    base_revision 为 None 表示旧客户端不带基线（单窗口兼容），直接写入。
    返回写入后的最新修订号。
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        current = get_plan_revision(db)
        if base_revision is not None and base_revision != current:
            raise RevisionConflict(current)
        db.execute("DELETE FROM schedule_items")
        db.executemany(
            "INSERT INTO schedule_items(zone_id,start_slot,length)"
            " VALUES(?,?,?)", clean)
        bump_plan_revision(db)
        db.execute("COMMIT")
    except RevisionConflict:
        db.execute("ROLLBACK")
        raise
    except Exception:
        db.execute("ROLLBACK")
        raise
    return get_plan_revision(db)


def build_state():
    db = get_db()
    valves = [valve_dict(r) for r in db.execute("SELECT * FROM valves ORDER BY id")]
    valve_by_id = {v["id"]: v for v in valves}
    zones = [zone_dict(r, valve_by_id)
             for r in db.execute("SELECT * FROM zones ORDER BY id")]
    blackouts = [blackout_dict(r)
                 for r in db.execute("SELECT * FROM blackouts ORDER BY id")]
    items = [item_dict(r)
             for r in db.execute("SELECT * FROM schedule_items ORDER BY start_slot")]
    settings = {r["key"]: r["value"]
                for r in db.execute("SELECT key,value FROM settings")}
    total_flow = float(settings.get("total_flow", 100))
    revision = int(settings.get("plan_revision", 1))
    evaluation = scheduler.evaluate(zones, valves, blackouts, items, total_flow)
    return {
        "revision": revision,
        "settings": {"plan_date": settings.get("plan_date", ""),
                     "total_flow": total_flow},
        "valves": valves,
        "zones": zones,
        "blackouts": blackouts,
        "items": items,
        "evaluation": evaluation,
    }


# ---------------------------------------------------------------- 校验工具

def bad(msg, code=400):
    return jsonify({"error": msg}), code


def revision_conflict(current_revision):
    """409：基线过期。响应附最新完整状态，供前端按分区三方对比。"""
    payload = {"error": "计划修订已过期：已有其他窗口先提交，本地草稿已保留，"
                        "请逐项复核差异后再提交",
               "conflict": "revision",
               "current_revision": current_revision,
               "state": build_state()}
    return jsonify(payload), 409


def parse_base_revision(data):
    """取可选基线修订：缺省（旧客户端）为 None 表示不做 CAS 检查。"""
    if "base_revision" not in data or data["base_revision"] in (None, ""):
        return None
    raw = data["base_revision"]
    # 只接受真正的 JSON 整数（拒绝 1.5、true、数字串等）
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("base_revision 必须是整数修订号")
    if raw < 1:
        raise ValueError("base_revision 必须为正整数")
    return raw


def require_fields(data, fields):
    for f in fields:
        if data.get(f) in (None, ""):
            raise ValueError(f"缺少字段：{f}")


def parse_time(data, key):
    if key not in data or data[key] in (None, ""):
        raise ValueError(f"缺少时间字段：{key}")
    return scheduler.hhmm_to_slot(data[key])


def parse_num(data, key, field_name, integer=False, positive=False):
    val = data.get(key)
    if val in (None, ""):
        raise ValueError(f"缺少字段：{field_name}")
    try:
        val = float(val)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} 必须是数字")
    if positive and val <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    if integer:
        if val != int(val) or int(val) < 1:
            raise ValueError(f"{field_name} 必须是正整数")
        return int(val)
    if val < 0:
        raise ValueError(f"{field_name} 不能为负")
    return val


# ---------------------------------------------------------------- 页面

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------- API: 状态 / 编排 / 导出

@app.get("/api/state")
def api_state():
    return jsonify(build_state())


@app.post("/api/generate")
def api_generate():
    data = request.get_json(silent=True) or {}
    try:
        base_revision = parse_base_revision(data)
    except ValueError as e:
        return bad(str(e))
    state = build_state()
    result = scheduler.generate(state["zones"], state["valves"],
                                state["blackouts"], state["settings"]["total_flow"])
    db = get_db()
    try:
        commit_schedule(db, [(it["zone_id"], it["start_slot"], it["length"])
                             for it in result["items"]], base_revision)
    except RevisionConflict as e:
        # 编排未落库；最新状态交回前端，由操作员选择保留草稿并按差异合并
        return revision_conflict(e.current_revision)
    return jsonify(build_state())


@app.put("/api/schedule")
def api_replace_schedule():
    data = request.get_json(silent=True) or {}
    raw_items = data.get("items", [])
    if not isinstance(raw_items, list):
        return bad("items 必须是数组")
    try:
        base_revision = parse_base_revision(data)
    except ValueError as e:
        return bad(str(e))
    db = get_db()
    zone_ids = {r["id"] for r in db.execute("SELECT id FROM zones")}
    seen = set()
    clean = []
    for i, it in enumerate(raw_items):
        zid = it.get("zone_id")
        start = it.get("start_slot")
        length = it.get("length")
        if zid not in zone_ids:
            return bad(f"第 {i + 1} 条：分区不存在")
        if zid in seen:
            return bad(f"第 {i + 1} 条：分区 {zid} 重复编排")
        seen.add(zid)
        if not isinstance(start, int) or not isinstance(length, int):
            return bad(f"第 {i + 1} 条：槽位必须为整数")
        if start < 0 or length < 1 or start + length > scheduler.SLOTS:
            return bad(f"第 {i + 1} 条：时段超出 00:00–24:00 范围")
        clean.append((zid, start, length))
    try:
        commit_schedule(db, clean, base_revision)
    except RevisionConflict as e:
        # 原子拒绝：DELETE/INSERT 均已回滚，服务器计划保持先提交者版本
        return revision_conflict(e.current_revision)
    return jsonify(build_state())


@app.get("/api/export")
def api_export():
    state = build_state()

    def num(x):
        """1500.0 -> 1500，保留真正的小数。"""
        return int(x) if float(x) == int(x) else float(x)

    zones_by_id = {z["id"]: z for z in state["zones"]}
    valve_by_id = {v["id"]: v for v in state["valves"]}
    ev = state["evaluation"]
    issue_by_zone = {i["zone_id"]: i["reasons"] for i in ev["issues"]}
    unsched_by_zone = {u["zone_id"]: u["reasons"] for u in ev["unscheduled"]}

    planned = []
    for it in state["items"]:
        z = zones_by_id[it["zone_id"]]
        v = valve_by_id.get(z["valve_id"])
        end = it["start_slot"] + it["length"]
        planned.append({
            "zone_id": z["id"],
            "zone": z["name"],
            "valve": v["name"] if v else None,
            "water_l": num(z["water_l"]),
            "start": scheduler.slot_to_hhmm(it["start_slot"]),
            "end": scheduler.slot_to_hhmm(end),
            "duration_min": it["length"] * scheduler.SLOT_MIN,
            "conflicts": issue_by_zone.get(z["id"], []),
        })
    planned.sort(key=lambda p: p["start"])

    payload = {
        "plan_date": state["settings"]["plan_date"],
        "plan_revision": state["revision"],
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "slot_minutes": scheduler.SLOT_MIN,
        "total_flow_limit_lmin": num(state["settings"]["total_flow"]),
        "actual_peak_flow_lmin": num(ev["max_flow"]),
        "valid": ev["valid"],
        "blackouts": [{"name": b["name"], "start": b["start"], "end": b["end"]}
                      for b in state["blackouts"]],
        "planned": planned,
        "unscheduled": [
            {"zone_id": u["zone_id"], "zone": u["zone_name"], "reasons": u["reasons"]}
            for u in ev["unscheduled"]
        ],
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"irrigation-plan-{state['settings']['plan_date']}.json"
    resp = Response(body, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = \
        f"attachment; filename=irrigation-plan.json; filename*=UTF-8''{fname}"
    return resp


# ---------------------------------------------------------------- API: 设置 / 重置

@app.post("/api/settings")
def api_settings():
    data = request.get_json(silent=True) or {}
    db = get_db()
    if "plan_date" in data and data["plan_date"]:
        db.execute("UPDATE settings SET value=? WHERE key='plan_date'",
                   (str(data["plan_date"]),))
    if "total_flow" in data and data["total_flow"] != "":
        try:
            total = parse_num(data, "total_flow", "总流量上限", positive=True)
        except ValueError as e:
            return bad(str(e))
        old = db.execute("SELECT value FROM settings WHERE key='total_flow'").fetchone()
        db.execute("UPDATE settings SET value=? WHERE key='total_flow'",
                   (str(total),))
        # 总流量上限变更使旧计划基线失效，避免在过期约束上合并
        if old is None or float(old["value"]) != total:
            bump_plan_revision(db)
    db.commit()
    return jsonify(build_state())


@app.post("/api/reset")
def api_reset():
    data = request.get_json(silent=True) or {}
    db = get_db()
    db.executescript(
        "DELETE FROM verify_events; DELETE FROM verify_batches; "
        "DELETE FROM drip_samples; DELETE FROM drip_zones; DELETE FROM drip_rounds; "
        "DELETE FROM fertilizer_pairs; DELETE FROM fertilizer_items; "
        "DELETE FROM fertilizer_recipes; "
        "DELETE FROM heat_readings; DELETE FROM heat_batches; "
        "DELETE FROM schedule_items; "
        # 全量重置轮灌对象：病害 / 水质历史保留，仅解除事件对旧分区的外键引用
        # （disease_events.zone_id 为 RESTRICT，直接删 zones 会触发外键约束）
        "UPDATE disease_events SET zone_id=NULL; "
        "DELETE FROM zones; "
        "DELETE FROM blackouts; DELETE FROM valves;"
    )
    if data.get("sample"):
        _seed_sample(db)  # 内部已贪心生成示例当日计划
        # 分区重建后补回水质 / 病害 / 积温 / 滴灌示例（水源与案件如已被用户保留则不重复）
        if db.execute("SELECT COUNT(*) FROM water_sources").fetchone()[0] == 0:
            _seed_water(db)
        _bind_sample_valve_sources(db)
        if db.execute("SELECT COUNT(*) FROM disease_tools").fetchone()[0] == 0:
            _seed_disease(db)
        if db.execute("SELECT COUNT(*) FROM heat_batches").fetchone()[0] == 0:
            _seed_heat(db)
        if db.execute("SELECT COUNT(*) FROM drip_rounds").fetchone()[0] == 0:
            _seed_drip(db)
        if db.execute("SELECT COUNT(*) FROM fertilizer_recipes").fetchone()[0] == 0:
            _seed_fertilizer(db)
        if db.execute("SELECT COUNT(*) FROM verify_batches").fetchone()[0] == 0:
            _seed_verify(db)
    else:
        db.execute("UPDATE settings SET value='100' WHERE key='total_flow'")
        db.commit()
    # 重置整体替换约束与计划：旧页面持有的任何基线一律失效
    bump_plan_revision(db)
    db.commit()
    return jsonify(build_state())


# ---------------------------------------------------------------- API: 阀门

def _parse_source_id(data, db, keep_if_absent=None):
    """解析阀门水源绑定。

    显式传 null / "" / 0 -> 解除绑定（None）；
    缺省字段且 keep_if_absent 给定时沿用原值（兼容轮灌页的阀门编辑器）；
    否则必须为已有水源 ID。
    """
    if "source_id" not in data:
        return keep_if_absent
    raw = data.get("source_id", None)
    if raw in (None, "", 0):
        return None
    try:
        sid = int(raw)
    except (TypeError, ValueError):
        raise ValueError("水源绑定必须为水源 ID")
    if not db.execute("SELECT id FROM water_sources WHERE id=?", (sid,)).fetchone():
        raise ValueError("绑定的水源不存在")
    return sid


@app.post("/api/valves")
def api_create_valve():
    data = request.get_json(silent=True) or {}
    try:
        require_fields(data, ["name"])
        flow = parse_num(data, "flow_lmin", "阀门额定流量", positive=True)
        concur = parse_num(data, "concur", "并发上限", integer=True)
        db = get_db()
        source_id = _parse_source_id(data, db)
        cur = db.execute(
            "INSERT INTO valves(name,flow_lmin,concur,source_id) VALUES(?,?,?,?)",
            (data["name"].strip(), flow, concur, source_id))
        # 新增阀门改变分区可选约束，旧基线失效
        bump_plan_revision(db)
        db.commit()
    except ValueError as e:
        return bad(str(e))
    except sqlite3.IntegrityError:
        return bad("阀门名称已存在")
    return jsonify({"id": cur.lastrowid}), 201


@app.put("/api/valves/<int:vid>")
def api_update_valve(vid, ):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT id,source_id FROM valves WHERE id=?", (vid,)).fetchone()
    if not row:
        return bad("阀门不存在", 404)
    try:
        require_fields(data, ["name"])
        flow = parse_num(data, "flow_lmin", "阀门额定流量", positive=True)
        concur = parse_num(data, "concur", "并发上限", integer=True)
        # 轮灌页的阀门编辑器不带 source_id：缺省即沿用当前绑定
        source_id = _parse_source_id(data, db, keep_if_absent=row["source_id"])
    except ValueError as e:
        return bad(str(e))
    try:
        old = db.execute(
            "SELECT flow_lmin,concur FROM valves WHERE id=?", (vid,)).fetchone()
        db.execute(
            "UPDATE valves SET name=?,flow_lmin=?,concur=?,source_id=? WHERE id=?",
            (data["name"].strip(), flow, concur, source_id, vid))
        # 额定流量 / 并发上限变更改变计划约束，使旧基线失效
        if (old["flow_lmin"] != flow or old["concur"] != concur):
            bump_plan_revision(db)
        db.commit()
    except sqlite3.IntegrityError:
        return bad("阀门名称已存在")
    return jsonify(build_state())


@app.put("/api/valves/<int:vid>/source")
def api_update_valve_source(vid):
    """仅修改阀门的水源绑定（水质页用），不触碰流量 / 并发 / 计划。"""
    data = request.get_json(silent=True) or {}
    db = get_db()
    if not db.execute("SELECT id FROM valves WHERE id=?", (vid,)).fetchone():
        return bad("阀门不存在", 404)
    try:
        source_id = _parse_source_id(data, db)
    except ValueError as e:
        return bad(str(e))
    db.execute("UPDATE valves SET source_id=? WHERE id=?", (source_id, vid))
    db.commit()
    # 绑定修改后用水回溯按新水源重新核算（计划本身不改动）
    return jsonify(build_state())


@app.delete("/api/valves/<int:vid>")
def api_delete_valve(vid):
    db = get_db()
    if not db.execute("SELECT 1 FROM valves WHERE id=?", (vid,)).fetchone():
        return bad("阀门不存在", 404)
    used = db.execute("SELECT COUNT(*) c FROM zones WHERE valve_id=?", (vid,)).fetchone()["c"]
    if used:
        return bad(f"仍有 {used} 个分区使用该阀门，请先改绑或删除分区")
    drip_used = db.execute(
        "SELECT COUNT(*) c FROM drip_rounds WHERE valve_id=?", (vid,)).fetchone()["c"]
    if drip_used:
        return bad(f"该阀门下仍有 {drip_used} 轮滴灌巡检记录，请先删除巡检轮次")
    verify_used = db.execute(
        "SELECT COUNT(*) c FROM verify_events WHERE valve_id=?", (vid,)).fetchone()["c"]
    if verify_used:
        return bad(f"该阀门在执行核验批次中仍有 {verify_used} 条运行事件，"
                   "请先删除对应事件或批次")
    db.execute("DELETE FROM valves WHERE id=?", (vid,))
    bump_plan_revision(db)
    db.commit()
    return jsonify(build_state())


# ---------------------------------------------------------------- API: 分区

def _read_zone_payload(data):
    require_fields(data, ["name", "valve_id"])
    water = parse_num(data, "water_l", "所需水量")
    earliest = parse_time(data, "earliest")
    latest = parse_time(data, "latest")
    if earliest >= latest:
        raise ValueError("最早灌溉时段必须早于最晚灌溉时段")
    valve_id = int(data["valve_id"])
    return data["name"].strip(), water, valve_id, earliest, latest


@app.post("/api/zones")
def api_create_zone():
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        name, water, valve_id, earliest, latest = _read_zone_payload(data)
        if not db.execute("SELECT id FROM valves WHERE id=?", (valve_id,)).fetchone():
            return bad("请选择有效阀门")
        cur = db.execute(
            "INSERT INTO zones(name,water_l,valve_id,earliest_slot,latest_slot)"
            " VALUES(?,?,?,?,?)", (name, water, valve_id, earliest, latest))
        # 新增分区改变计划对象集，旧基线失效
        bump_plan_revision(db)
        db.commit()
    except ValueError as e:
        return bad(str(e))
    except sqlite3.IntegrityError:
        return bad("分区名称已存在")
    return jsonify({"id": cur.lastrowid}), 201


@app.put("/api/zones/<int:zid>")
def api_update_zone(zid):
    data = request.get_json(silent=True) or {}
    db = get_db()
    if not db.execute("SELECT id FROM zones WHERE id=?", (zid,)).fetchone():
        return bad("分区不存在", 404)
    try:
        name, water, valve_id, earliest, latest = _read_zone_payload(data)
        if not db.execute("SELECT id FROM valves WHERE id=?", (valve_id,)).fetchone():
            return bad("请选择有效阀门")
        old = db.execute(
            "SELECT water_l,valve_id,earliest_slot,latest_slot FROM zones"
            " WHERE id=?", (zid,)).fetchone()
        db.execute(
            "UPDATE zones SET name=?,water_l=?,valve_id=?,earliest_slot=?,"
            "latest_slot=? WHERE id=?",
            (name, water, valve_id, earliest, latest, zid))
        # 水量 / 阀门 / 允许时段（分区约束）变更使旧基线失效
        if (old["water_l"] != water or old["valve_id"] != valve_id
                or old["earliest_slot"] != earliest
                or old["latest_slot"] != latest):
            bump_plan_revision(db)
        db.commit()
    except ValueError as e:
        return bad(str(e))
    except sqlite3.IntegrityError:
        return bad("分区名称已存在")
    return jsonify(build_state())


@app.delete("/api/zones/<int:zid>")
def api_delete_zone(zid):
    db = get_db()
    used = db.execute(
        "SELECT c.id FROM disease_events e JOIN disease_cases c ON c.id=e.case_id"
        " WHERE e.zone_id=? AND c.status='open' LIMIT 1", (zid,)).fetchone()
    if used:
        return bad(f"该分区在未冻结病害案件 #{used['id']} 中已有事件，"
                   "请先冻结案件后再删除分区")
    heat_used = db.execute(
        "SELECT COUNT(*) c FROM heat_batches WHERE zone_id=?", (zid,)).fetchone()["c"]
    if heat_used:
        return bad(f"该分区下仍有 {heat_used} 个积温批次，请先删除批次后再删除分区")
    drip_used = db.execute(
        "SELECT COUNT(*) c FROM drip_zones WHERE zone_id=?", (zid,)).fetchone()["c"]
    if drip_used:
        return bad(f"该分区在 {drip_used} 轮滴灌均匀度巡检中已有记录，"
                   "请先删除对应巡检轮次后再删除分区")
    fert_used = db.execute(
        "SELECT COUNT(*) c FROM fertilizer_recipes WHERE zone_id=?", (zid,)).fetchone()["c"]
    if fert_used:
        return bad(f"该分区下仍有 {fert_used} 份肥料母液配方，"
                   "请先删除配方后再删除分区")
    db.execute("DELETE FROM schedule_items WHERE zone_id=?", (zid,))
    # 冻结案件的重放结果已固化为快照，历史事件解除分区引用后即可删除
    db.execute("UPDATE disease_events SET zone_id=NULL WHERE zone_id=?", (zid,))
    db.execute("DELETE FROM zones WHERE id=?", (zid,))
    bump_plan_revision(db)
    db.commit()
    return jsonify(build_state())


# ---------------------------------------------------------------- API: 停水

@app.post("/api/blackouts")
def api_create_blackout():
    data = request.get_json(silent=True) or {}
    try:
        name = (data.get("name") or "停水时段").strip()
        start = parse_time(data, "start")
        end = parse_time(data, "end")
        if start >= end:
            raise ValueError("停水开始时间必须早于结束时间")
    except ValueError as e:
        return bad(str(e))
    db = get_db()
    cur = db.execute("INSERT INTO blackouts(name,start_slot,end_slot) VALUES(?,?,?)",
                     (name, start, end))
    # 停水时段是计划约束：新增即令旧基线失效
    bump_plan_revision(db)
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.put("/api/blackouts/<int:bid>")
def api_update_blackout(bid):
    data = request.get_json(silent=True) or {}
    db = get_db()
    if not db.execute("SELECT id FROM blackouts WHERE id=?", (bid,)).fetchone():
        return bad("停水时段不存在", 404)
    try:
        name = (data.get("name") or "停水时段").strip()
        start = parse_time(data, "start")
        end = parse_time(data, "end")
        if start >= end:
            raise ValueError("停水开始时间必须早于结束时间")
    except ValueError as e:
        return bad(str(e))
    db.execute("UPDATE blackouts SET name=?,start_slot=?,end_slot=? WHERE id=?",
               (name, start, end, bid))
    bump_plan_revision(db)
    db.commit()
    return jsonify(build_state())


@app.delete("/api/blackouts/<int:bid>")
def api_delete_blackout(bid):
    db = get_db()
    db.execute("DELETE FROM blackouts WHERE id=?", (bid,))
    bump_plan_revision(db)
    db.commit()
    return jsonify(build_state())


# ---------------------------------------------------------------- API: 水质监测 - 状态构建

def _ranges_for(db, sid):
    """读取某水源的指标区间；缺项用默认值补齐。"""
    rows = db.execute(
        "SELECT metric,low,high FROM water_ranges WHERE source_id=?", (sid,))
    ranges = {r["metric"]: (r["low"], r["high"]) for r in rows}
    for key in wq.METRIC_KEYS:
        ranges.setdefault(key, (wq.METRICS[key][2], wq.METRICS[key][3]))
    return ranges


def _recomputed_samples(db, sid, ranges):
    """取某水源全部采样并按时间重算每个采样点的指标/整体状态。"""
    rows = db.execute(
        "SELECT id,source_id,ts,ph,ec,turbidity,nitrate FROM water_samples"
        " WHERE source_id=? ORDER BY ts,id", (sid,))
    samples = [{k: r[k] for k in
                ("id", "source_id", "ts", "ph", "ec", "turbidity", "nitrate")}
               for r in rows]
    samples, _summary = wq.recompute_source(samples, ranges)
    return samples


def _source_card(db, row, ranges):
    samples = _recomputed_samples(db, row["id"], ranges)
    summary = {
        "id": row["id"], "name": row["name"], "note": row["note"],
        "created": row["created"], "sample_count": len(samples),
    }
    if samples:
        last = samples[-1]
        summary.update(status=last["status"], triggers=last["triggers"],
                       last_ts=last["ts"], metric_states=last["metric_states"])
    else:
        summary.update(status=wq.NORMAL, triggers=[], last_ts=None,
                       metric_states={k: wq.NORMAL for k in wq.METRIC_KEYS})
    return summary


def build_water_state(source_id=None, day=None, limit=50):
    db = get_db()
    source_rows = db.execute(
        "SELECT * FROM water_sources ORDER BY id").fetchall()
    if source_id is None and source_rows:
        source_id = source_rows[0]["id"]
    selected = next((r for r in source_rows if r["id"] == source_id), None)
    if source_id is not None and selected is None:
        raise KeyError("水源不存在")

    cards = []
    for r in source_rows:
        cards.append(_source_card(db, r, _ranges_for(db, r["id"])))

    ranges = _ranges_for(db, source_id) if selected else wq.default_ranges()
    metrics_meta = [{
        "key": k, "label": wq.METRICS[k][0], "unit": wq.METRICS[k][1],
        "low": ranges[k][0], "high": ranges[k][1],
    } for k in wq.METRIC_KEYS]

    samples = []
    if selected:
        all_samples = _recomputed_samples(db, selected["id"], ranges)
        if day:
            samples = [s for s in all_samples if s["ts"][:10] == day]
        elif limit and limit > 0:
            samples = all_samples[-limit:]
        else:
            samples = all_samples

    plan_date = db.execute(
        "SELECT value FROM settings WHERE key='plan_date'").fetchone()
    valves = [valve_dict(r) for r in db.execute(
        "SELECT * FROM valves ORDER BY id")]
    source_names = {r["id"]: r["name"] for r in source_rows}
    for v in valves:
        v["source_name"] = source_names.get(v["source_id"])

    return {
        "metrics": metrics_meta,
        "overall_status": wq.merge_status([c["status"] for c in cards]),
        "sources": cards,
        "selected_id": selected["id"] if selected else None,
        "date": day or "",
        "plan_date": plan_date["value"] if plan_date else "",
        "valves": valves,
        "ranges": {k: {"low": ranges[k][0], "high": ranges[k][1]}
                   for k in wq.METRIC_KEYS},
        "samples": samples,
    }


def build_water_export(source_id=None, day=None):
    state = build_water_state(source_id=source_id, day=day, limit=0)
    return {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "source_id": state["selected_id"],
        "source": next((s["name"] for s in state["sources"]
                        if s["id"] == state["selected_id"]), None),
        "date": state["date"],
        "overall_status": state["overall_status"],
        "ranges": state["ranges"],
        "metric_units": {k: wq.METRICS[k][1] for k in wq.METRIC_KEYS},
        "metric_labels": {k: wq.METRICS[k][0] for k in wq.METRIC_KEYS},
        "sources": [
            {"id": s["id"], "name": s["name"], "status": s["status"],
             "triggers": s["triggers"], "last_ts": s["last_ts"],
             "sample_count": s["sample_count"]}
            for s in state["sources"]
        ],
        "samples": state["samples"],
    }


# ---------------------------------------------------------------- API: 水质监测 - 用水回溯

def build_water_trace(day=None, zone_id=None):
    """按计划日期把现有 15 分钟灌溉时段与各水源半开状态区间求交。

    纯按需核算：读取当前计划、阀门绑定、水源区间与全部采样，
    实时调用 water_trace.trace_day；不写入、不改动原计划。
    """
    db = get_db()
    settings = {r["key"]: r["value"]
                for r in db.execute("SELECT key,value FROM settings")}
    day_raw = day or settings.get("plan_date") or date.today().isoformat()
    try:
        day_dt = wt.parse_day(day_raw)
    except ValueError:
        raise ValueError(f"计划日期格式错误：{day_raw}（应为 YYYY-MM-DD）")

    if zone_id is not None and not db.execute(
            "SELECT id FROM zones WHERE id=?", (zone_id,)).fetchone():
        raise KeyError("分区不存在")

    valve_rows = db.execute("SELECT * FROM valves ORDER BY id").fetchall()
    valves = [valve_dict(r) for r in valve_rows]
    zones = [dict(id=r["id"], name=r["name"], valve_id=r["valve_id"])
             for r in db.execute("SELECT id,name,valve_id FROM zones ORDER BY id")]
    items = [dict(zone_id=r["zone_id"], start_slot=r["start_slot"],
                  length=r["length"])
             for r in db.execute(
                 "SELECT zone_id,start_slot,length FROM schedule_items"
                 " ORDER BY start_slot,zone_id")]

    source_rows = db.execute("SELECT id,name FROM water_sources ORDER BY id").fetchall()
    source_names = {r["id"]: r["name"] for r in source_rows}
    samples_by_source = {}
    ranges_by_source = {}
    for r in source_rows:
        sid = r["id"]
        ranges_by_source[sid] = _ranges_for(db, sid)
        samples_by_source[sid] = [
            {k: row[k] for k in
             ("id", "source_id", "ts", "ph", "ec", "turbidity", "nitrate")}
            for row in db.execute(
                "SELECT id,source_id,ts,ph,ec,turbidity,nitrate FROM water_samples"
                " WHERE source_id=? ORDER BY ts,id", (sid,))]

    result = wt.trace_day(
        zones, items, valves, samples_by_source, ranges_by_source,
        day_dt, slot_min=scheduler.SLOT_MIN, source_names=source_names,
        zone_filter=zone_id)
    result["plan_date"] = settings.get("plan_date", "")
    return result


def build_water_trace_export(day=None, zone_id=None):
    trace = build_water_trace(day=day, zone_id=zone_id)
    return {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "plan_date": trace["plan_date"],
        "date": trace["date"],
        "slot_minutes": trace["slot_minutes"],
        "summary": trace["summary"],
        "unbound_valves": trace["unbound_valves"],
        "zones": trace["zones"],
    }


# ---------------------------------------------------------------- 页面

@app.route("/water")
def water_page():
    return send_from_directory(app.static_folder, "water.html")


# ---------------------------------------------------------------- API: 水源

@app.get("/api/water/state")
def api_water_state():
    source_id = request.args.get("source_id", type=int)
    day = (request.args.get("date") or "").strip() or None
    limit = request.args.get("limit", default=50, type=int)
    try:
        return jsonify(build_water_state(source_id, day, limit))
    except KeyError:
        return bad("水源不存在", 404)


@app.post("/api/water/sources")
def api_water_create_source():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return bad("缺少字段：水源名称")
    note = (data.get("note") or "").strip()
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO water_sources(name,note,created) VALUES(?,?,?)",
            (name, note, datetime.now().replace(microsecond=0).isoformat()))
        sid = cur.lastrowid
        for key in wq.METRIC_KEYS:
            db.execute(
                "INSERT INTO water_ranges(source_id,metric,low,high)"
                " VALUES(?,?,?,?)",
                (sid, key, wq.METRICS[key][2], wq.METRICS[key][3]))
        db.commit()
    except sqlite3.IntegrityError:
        return bad("水源名称已存在")
    return jsonify(build_water_state(sid)), 201


@app.put("/api/water/sources/<int:sid>")
def api_water_update_source(sid):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return bad("缺少字段：水源名称")
    note = (data.get("note") or "").strip()
    db = get_db()
    if not db.execute("SELECT id FROM water_sources WHERE id=?", (sid,)).fetchone():
        return bad("水源不存在", 404)
    try:
        db.execute("UPDATE water_sources SET name=?,note=? WHERE id=?",
                   (name, note, sid))
        db.commit()
    except sqlite3.IntegrityError:
        return bad("水源名称已存在")
    return jsonify(build_water_state(sid))


@app.delete("/api/water/sources/<int:sid>")
def api_water_delete_source(sid):
    db = get_db()
    if not db.execute("SELECT id FROM water_sources WHERE id=?", (sid,)).fetchone():
        return bad("水源不存在", 404)
    db.execute("DELETE FROM water_samples WHERE source_id=?", (sid,))
    db.execute("DELETE FROM water_ranges WHERE source_id=?", (sid,))
    # 阀门绑定经外键 ON DELETE SET NULL 自动解除（双保险显式置空）
    db.execute("UPDATE valves SET source_id=NULL WHERE source_id=?", (sid,))
    db.execute("DELETE FROM water_sources WHERE id=?", (sid,))
    db.commit()
    return jsonify(build_water_state())


# ---------------------------------------------------------------- API: 区间配置

@app.put("/api/water/sources/<int:sid>/ranges")
def api_water_update_ranges(sid):
    data = request.get_json(silent=True) or {}
    raw = data.get("ranges", data)
    db = get_db()
    if not db.execute("SELECT id FROM water_sources WHERE id=?", (sid,)).fetchone():
        return bad("水源不存在", 404)
    try:
        ranges = wq.normalize_ranges(raw)
    except ValueError as e:
        return bad(str(e))
    for key, (lo, hi) in ranges.items():
        db.execute(
            "UPDATE water_ranges SET low=?,high=? WHERE source_id=? AND metric=?",
            (lo, hi, sid, key))
    db.commit()
    # 区间变更后按水源按时间整体重算状态
    return jsonify(build_water_state(sid))


# ---------------------------------------------------------------- API: 采样录入

@app.post("/api/water/sources/<int:sid>/samples")
def api_water_add_samples(sid):
    data = request.get_json(silent=True)
    db = get_db()
    if not db.execute("SELECT id FROM water_sources WHERE id=?", (sid,)).fetchone():
        return bad("水源不存在", 404)
    ranges = _ranges_for(db, sid)
    latest_row = db.execute(
        "SELECT ts FROM water_samples WHERE source_id=? ORDER BY ts DESC,id DESC"
        " LIMIT 1", (sid,)).fetchone()
    latest_ts = latest_row["ts"] if latest_row else None
    try:
        if data is None:
            raise ValueError("请求体必须为 JSON")
        cleaned = wq.validate_batch(data, ranges, latest_ts)
    except ValueError as e:
        return bad(str(e))
    try:
        for row in cleaned:
            db.execute(
                "INSERT INTO water_samples(source_id,ts,ph,ec,turbidity,nitrate)"
                " VALUES(?,?,?,?,?,?)",
                (sid, row["ts"], row["ph"], row["ec"], row["turbidity"],
                 row["nitrate"]))
        db.commit()
    except sqlite3.IntegrityError:
        return bad("时间戳重复，请检查批次")
    return jsonify(build_water_state(sid)), 201


@app.delete("/api/water/samples/<int:sample_id>")
def api_water_delete_sample(sample_id):
    db = get_db()
    row = db.execute("SELECT source_id FROM water_samples WHERE id=?",
                     (sample_id,)).fetchone()
    if not row:
        return bad("采样不存在", 404)
    db.execute("DELETE FROM water_samples WHERE id=?", (sample_id,))
    db.commit()
    # 删除后需重算该水源后续采样状态
    return jsonify(build_water_state(row["source_id"]))


# ---------------------------------------------------------------- API: 导出

@app.get("/api/water/trace")
def api_water_trace():
    day = (request.args.get("date") or "").strip() or None
    zone_id = request.args.get("zone_id", type=int)
    try:
        return jsonify(build_water_trace(day=day, zone_id=zone_id))
    except KeyError as e:
        return bad(str(e).strip("'"), 404)
    except ValueError as e:
        return bad(str(e))


@app.get("/api/water/trace/export")
def api_water_trace_export():
    day = (request.args.get("date") or "").strip() or None
    zone_id = request.args.get("zone_id", type=int)
    try:
        payload = build_water_trace_export(day=day, zone_id=zone_id)
    except KeyError as e:
        return bad(str(e).strip("'"), 404)
    except ValueError as e:
        return bad(str(e))
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    tag = payload["date"]
    ztag = zone_id or "all"
    fname = f"water-trace-{tag}-zone{ztag}.json"
    resp = Response(body, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = \
        f"attachment; filename=water-trace.json; filename*=UTF-8''{fname}"
    return resp


@app.get("/api/water/export")
def api_water_export():
    source_id = request.args.get("source_id", type=int)
    day = (request.args.get("date") or "").strip() or None
    try:
        payload = build_water_export(source_id, day)
    except KeyError:
        return bad("水源不存在", 404)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    tag = day or "all"
    fname = f"water-quality-{payload['source_id'] or 'all'}-{tag}.json"
    resp = Response(body, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = \
        f"attachment; filename=water-quality.json; filename*=UTF-8''{fname}"
    return resp


# ---------------------------------------------------------------- API: 病害接触追踪

def _tool_dict(row):
    return {"id": row["id"], "name": row["name"], "note": row["note"],
            "retired": bool(row["retired"])}


def _case_dict(db, row):
    snap = db.execute("SELECT frozen_at FROM disease_snapshots WHERE case_id=?",
                      (row["id"],)).fetchone()
    return {
        "id": row["id"],
        "disease_name": row["disease_name"],
        "latency_hours": row["latency_hours"],
        "status": row["status"],
        "note": row["note"],
        "created": row["created"],
        "frozen_at": snap["frozen_at"] if snap else None,
        "event_count": db.execute(
            "SELECT COUNT(*) c FROM disease_events WHERE case_id=?",
            (row["id"],)).fetchone()["c"],
    }


def _event_dict(row, tool_by_id, zone_by_id, frozen=False):
    tool = tool_by_id.get(row["tool_id"]) if row["tool_id"] is not None else None
    zone = zone_by_id.get(row["zone_id"]) if row["zone_id"] is not None else None
    if frozen:
        # 冻结案件以事件落库时固化的名称为准（工具可能已被删除 / 改名）
        tool_name = (row["tool_name"] if row["tool_name"] not in (None, "")
                     else (tool["name"] if tool else None))
    else:
        tool_name = tool["name"] if tool else row["tool_name"]
    return {
        "id": row["id"], "case_id": row["case_id"], "type": row["type"],
        "type_label": dt.TYPE_LABEL[row["type"]], "ts": row["ts"],
        "tool_id": row["tool_id"],
        "tool_name": tool_name,
        "zone_id": row["zone_id"],
        "zone_name": zone["name"] if zone else None,
        "qualified": bool(row["qualified"]), "note": row["note"],
    }


def _raw_events(db, case_id, tool_by_id, zone_by_id, frozen=False):
    rows = db.execute(
        "SELECT * FROM disease_events WHERE case_id=? ORDER BY ts,id", (case_id,))
    return [_event_dict(r, tool_by_id, zone_by_id, frozen) for r in rows]


def _frozen_history_tools(db, case_id, result, live_ids):
    """冻结案件引用、但工具表已删除的工具（编号 + 冻结时名称）。

    从事件台账与冻结快照（传播链中的工具 / 结果工具表）聚合，
    保证工具删除后冻结案件仍能按编号筛选、导出且名称不丢。
    """
    hist = {}
    for r in db.execute(
            "SELECT tool_id, tool_name FROM disease_events"
            " WHERE case_id=? AND tool_id IS NOT NULL", (case_id,)):
        if r["tool_id"] not in live_ids and r["tool_name"]:
            hist[r["tool_id"]] = r["tool_name"]
    if result is not None:
        chains = ([c for z in result.get("zones", []) for c in z.get("chain", [])]
                  + [c for t in result.get("tools", []) for c in t.get("chain", [])])
        for c in chains:
            tid = c.get("tool_id")
            if tid is not None and tid not in live_ids and c.get("tool_name"):
                hist.setdefault(tid, c["tool_name"])
        for t in result.get("tools", []):
            tid = t.get("tool_id")
            if tid is not None and tid not in live_ids and t.get("tool_name"):
                hist.setdefault(tid, t["tool_name"])
    return [{"id": tid, "name": name, "note": "", "retired": False, "deleted": True}
            for tid, name in sorted(hist.items())]


def _replay_case(db, case_row):
    """对一个案件按时间重算；已冻结案件读取冻结快照。"""
    tools = [_tool_dict(r) for r in db.execute(
        "SELECT * FROM disease_tools ORDER BY id")]
    zones = [{"id": r["id"], "name": r["name"]}
             for r in db.execute("SELECT id,name FROM zones ORDER BY id")]
    tool_by_id = {t["id"]: t for t in tools}
    zone_by_id = {z["id"]: z for z in zones}
    is_frozen = case_row["status"] == dt.FROZEN
    events = _raw_events(db, case_row["id"], tool_by_id, zone_by_id, is_frozen)

    if is_frozen:
        snap = db.execute("SELECT payload FROM disease_snapshots WHERE case_id=?",
                          (case_row["id"],)).fetchone()
        result = json.loads(snap["payload"]) if snap else None
        tools = tools + _frozen_history_tools(db, case_row["id"], result,
                                              set(tool_by_id))
    else:
        result = dt.replay(events, case_row["latency_hours"], zones, tools)
    return events, tools, zones, result


def build_disease_state(case_id=None):
    db = get_db()
    cases = [_case_dict(db, r)
             for r in db.execute("SELECT * FROM disease_cases ORDER BY id")]
    if case_id is None and cases:
        # 默认选中最近的未冻结案件，否则取最新一条
        open_case = next((c for c in reversed(cases) if c["status"] == dt.OPEN), None)
        case_id = open_case["id"] if open_case else cases[-1]["id"]
    case_row = db.execute("SELECT * FROM disease_cases WHERE id=?",
                          (case_id,)).fetchone() if case_id is not None else None
    if case_id is not None and case_row is None:
        raise KeyError("案件不存在")

    selected = None
    events = []
    zones = [{"id": r["id"], "name": r["name"]}
             for r in db.execute("SELECT id,name FROM zones ORDER BY id")]
    tools = [_tool_dict(r) for r in db.execute(
        "SELECT * FROM disease_tools ORDER BY id")]
    if case_row:
        evs, _tools, _zones, result = _replay_case(db, case_row)
        events, tools, zones = evs, _tools, _zones
        selected = {
            **_case_dict(db, case_row),
            "result": result,
        }
    return {"cases": cases, "selected_id": case_row["id"] if case_row else None,
            "selected": selected, "events": events,
            "zones": zones, "tools": tools}


def _require_open_case(db, case_id):
    row = db.execute("SELECT * FROM disease_cases WHERE id=?", (case_id,)).fetchone()
    if row is None:
        return None, bad("案件不存在", 404)
    if row["status"] == dt.FROZEN:
        return None, bad("案件已冻结，不能再补录 / 删除事件；请新建案件", 409)
    return row, None


def _read_event_payload(db, data):
    etype = (data.get("type") or "").strip()
    if etype not in dt.EVENT_TYPES:
        raise ValueError("事件类型必须为 diagnosis / visit / disinfection")
    try:
        ts = dt.parse_ts(data.get("ts"))
    except ValueError as e:
        raise ValueError(str(e))
    tool_id, zone_id = data.get("tool_id"), data.get("zone_id")
    try:
        tool_id = int(tool_id) if tool_id not in (None, "") else None
        zone_id = int(zone_id) if zone_id not in (None, "") else None
    except (TypeError, ValueError):
        raise ValueError("工具 / 分区编号非法")
    if etype == dt.DIAGNOSIS:
        if zone_id is None:
            raise ValueError("确诊事件必须指定分区")
    elif etype == dt.VISIT:
        if tool_id is None or zone_id is None:
            raise ValueError("到访事件必须同时指定工具与分区")
    else:
        if tool_id is None:
            raise ValueError("消毒事件必须指定工具")
    tool_row = None
    if tool_id is not None:
        tool_row = db.execute(
            "SELECT name FROM disease_tools WHERE id=?", (tool_id,)).fetchone()
        if not tool_row:
            raise ValueError("请选择有效工具")
    if zone_id is not None and not db.execute(
            "SELECT 1 FROM zones WHERE id=?", (zone_id,)).fetchone():
        raise ValueError("请选择现有温室分区")
    qualified = 1 if data.get("qualified") in (True, 1, "1", "true", "on") else 0
    if etype != dt.DISINFECTION:
        qualified = 0
    note = (data.get("note") or "").strip()
    tool_name = tool_row["name"] if tool_row else None
    return etype, ts, tool_id, tool_name, zone_id, qualified, note


@app.route("/disease")
def disease_page():
    return send_from_directory(app.static_folder, "disease.html")


@app.get("/api/disease/state")
def api_disease_state():
    case_id = request.args.get("case_id", type=int)
    try:
        return jsonify(build_disease_state(case_id))
    except KeyError:
        return bad("案件不存在", 404)


@app.get("/api/disease/export")
def api_disease_export():
    case_id = request.args.get("case_id", type=int)
    try:
        state = build_disease_state(case_id)
    except KeyError:
        return bad("案件不存在", 404)
    if state["selected"] is None:
        return bad("没有可导出的案件")
    sel = state["selected"]
    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "case": {
            "id": sel["id"], "disease_name": sel["disease_name"],
            "latency_hours": sel["latency_hours"], "status": sel["status"],
            "note": sel["note"], "created": sel["created"],
            "frozen_at": sel["frozen_at"],
        },
        "zones": state["zones"],
        "tools": state["tools"],
        "events": state["events"],
        "result": sel["result"],
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"disease-case-{sel['id']}-{sel['status']}.json"
    resp = Response(body, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = \
        f"attachment; filename=disease-case.json; filename*=UTF-8''{fname}"
    return resp


# ---- 工具 ----

@app.post("/api/disease/tools")
def api_disease_create_tool():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return bad("缺少字段：工具名称")
    db = get_db()
    try:
        cur = db.execute("INSERT INTO disease_tools(name,note) VALUES(?,?)",
                         (name, (data.get("note") or "").strip()))
        db.commit()
    except sqlite3.IntegrityError:
        return bad("工具名称已存在")
    return jsonify(build_disease_state()), 201


@app.put("/api/disease/tools/<int:tool_id>")
def api_disease_update_tool(tool_id):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return bad("缺少字段：工具名称")
    db = get_db()
    if not db.execute("SELECT 1 FROM disease_tools WHERE id=?", (tool_id,)).fetchone():
        return bad("工具不存在", 404)
    try:
        db.execute("UPDATE disease_tools SET name=?,note=?,retired=? WHERE id=?",
                   (name, (data.get("note") or "").strip(),
                    1 if data.get("retired") else 0, tool_id))
        db.commit()
    except sqlite3.IntegrityError:
        return bad("工具名称已存在")
    return jsonify(build_disease_state())


@app.delete("/api/disease/tools/<int:tool_id>")
def api_disease_delete_tool(tool_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM disease_tools WHERE id=?", (tool_id,)).fetchone():
        return bad("工具不存在", 404)
    used = db.execute(
        "SELECT c.id FROM disease_events e JOIN disease_cases c ON c.id=e.case_id"
        " WHERE e.tool_id=? AND c.status='open' LIMIT 1", (tool_id,)).fetchone()
    if used:
        return bad(f"该工具在未冻结案件 #{used['id']} 中已有到访/消毒事件，"
                   "请先冻结案件后再删除")
    # 冻结案件的工具编号与名称已固化在事件台账（tool_id/tool_name）与冻结快照中，
    # 直接删除工具即可，不解除、不改写历史事件引用。
    db.execute("DELETE FROM disease_tools WHERE id=?", (tool_id,))
    db.commit()
    return jsonify(build_disease_state())


# ---- 案件 ----

@app.post("/api/disease/cases")
def api_disease_create_case():
    data = request.get_json(silent=True) or {}
    name = (data.get("disease_name") or "").strip()
    if not name:
        return bad("缺少字段：病害名称")
    try:
        latency = dt.normalize_latency(data.get("latency_hours"))
    except ValueError as e:
        return bad(str(e))
    db = get_db()
    cur = db.execute(
        "INSERT INTO disease_cases(disease_name,latency_hours,status,note,created)"
        " VALUES(?,?,'open',?,?)",
        (name, latency, (data.get("note") or "").strip(),
         datetime.now().replace(microsecond=0).isoformat()))
    db.commit()
    return jsonify(build_disease_state(cur.lastrowid)), 201


@app.put("/api/disease/cases/<int:case_id>")
def api_disease_update_case(case_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row, err = _require_open_case(db, case_id)
    if err:
        return err
    name = (data.get("disease_name") or "").strip()
    if not name:
        return bad("缺少字段：病害名称")
    latency = row["latency_hours"]
    if data.get("latency_hours") not in (None, ""):
        try:
            latency = dt.normalize_latency(data.get("latency_hours"))
        except ValueError as e:
            return bad(str(e))
    db.execute("UPDATE disease_cases SET disease_name=?,latency_hours=?,note=? WHERE id=?",
               (name, latency, (data.get("note") or "").strip(), case_id))
    db.commit()
    return jsonify(build_disease_state(case_id))


@app.post("/api/disease/cases/<int:case_id>/freeze")
def api_disease_freeze_case(case_id):
    db = get_db()
    row, err = _require_open_case(db, case_id)
    if err:
        return err
    frozen_at = datetime.now().replace(microsecond=0).isoformat()
    # 冻结前把工具当时的名称固化到事件台账：日后工具被删除 / 改名，
    # 冻结案件仍按冻结时的编号与名称展示、导出。
    db.execute(
        "UPDATE disease_events SET tool_name ="
        " (SELECT name FROM disease_tools WHERE disease_tools.id = disease_events.tool_id)"
        " WHERE case_id=? AND tool_id IS NOT NULL", (case_id,))
    events, _tools, zones, result = _replay_case(db, row)
    payload = json.dumps(result, ensure_ascii=False)
    db.execute("UPDATE disease_cases SET status='frozen',frozen_at=? WHERE id=?",
               (frozen_at, case_id))
    db.execute(
        "INSERT INTO disease_snapshots(case_id,payload,frozen_at) VALUES(?,?,?)",
        (case_id, payload, frozen_at))
    db.commit()
    return jsonify(build_disease_state(case_id))


@app.delete("/api/disease/cases/<int:case_id>")
def api_disease_delete_case(case_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM disease_cases WHERE id=?", (case_id,)).fetchone():
        return bad("案件不存在", 404)
    db.execute("DELETE FROM disease_events WHERE case_id=?", (case_id,))
    db.execute("DELETE FROM disease_snapshots WHERE case_id=?", (case_id,))
    db.execute("DELETE FROM disease_cases WHERE id=?", (case_id,))
    db.commit()
    return jsonify(build_disease_state())


# ---- 事件 ----

@app.post("/api/disease/cases/<int:case_id>/events")
def api_disease_create_event(case_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    _row, err = _require_open_case(db, case_id)
    if err:
        return err
    try:
        etype, ts, tool_id, tool_name, zone_id, qualified, note = _read_event_payload(db, data)
    except ValueError as e:
        return bad(str(e))
    db.execute(
        "INSERT INTO disease_events(case_id,type,ts,tool_id,tool_name,zone_id,qualified,note,created)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (case_id, etype, ts, tool_id, tool_name, zone_id, qualified, note,
         datetime.now().replace(microsecond=0).isoformat()))
    db.commit()
    return jsonify(build_disease_state(case_id)), 201


@app.put("/api/disease/events/<int:event_id>")
def api_disease_update_event(event_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM disease_events WHERE id=?", (event_id,)).fetchone()
    if not row:
        return bad("事件不存在", 404)
    _case, err = _require_open_case(db, row["case_id"])
    if err:
        return err
    try:
        etype, ts, tool_id, tool_name, zone_id, qualified, note = _read_event_payload(db, data)
    except ValueError as e:
        return bad(str(e))
    db.execute(
        "UPDATE disease_events SET type=?,ts=?,tool_id=?,tool_name=?,zone_id=?,qualified=?,"
        "note=? WHERE id=?",
        (etype, ts, tool_id, tool_name, zone_id, qualified, note, event_id))
    db.commit()
    return jsonify(build_disease_state(row["case_id"]))


@app.delete("/api/disease/events/<int:event_id>")
def api_disease_delete_event(event_id):
    db = get_db()
    row = db.execute("SELECT case_id FROM disease_events WHERE id=?",
                     (event_id,)).fetchone()
    if not row:
        return bad("事件不存在", 404)
    _case, err = _require_open_case(db, row["case_id"])
    if err:
        return err
    db.execute("DELETE FROM disease_events WHERE id=?", (event_id,))
    db.commit()
    return jsonify(build_disease_state(row["case_id"]))


# ---------------------------------------------------------------- API: 作物积温预测

def _heat_batch_dict(row, result=None):
    return {
        "id": row["id"],
        "zone_id": row["zone_id"],
        "name": row["name"],
        "crop": row["crop"],
        "base_temp": row["base_temp"],
        "cap_temp": row["cap_temp"],
        "target_gdd": row["target_gdd"],
        "max_gap_hours": row["max_gap_hours"],
        "created": row["created"],
        "result": result,
    }


def _heat_config(row):
    return {
        "base_temp": row["base_temp"], "cap_temp": row["cap_temp"],
        "target_gdd": row["target_gdd"], "max_gap_hours": row["max_gap_hours"],
    }


def _heat_compute(db, batch_row):
    """取该批次全部读数，按时间整体重算。"""
    rows = db.execute(
        "SELECT id,ts,temp FROM heat_readings WHERE batch_id=? ORDER BY ts,id",
        (batch_row["id"],)).fetchall()
    readings = [{"id": r["id"], "ts": r["ts"], "temp": r["temp"]} for r in rows]
    result = ch.compute(_heat_config(batch_row), readings)
    result["readings"] = readings
    return result


def _heat_card(db, row):
    """批次列表卡：带轻量汇总（不附全部读数 / 日台账）。"""
    res = _heat_compute(db, row)
    return _heat_batch_dict(row, {
        "reading_count": res["reading_count"],
        "first_ts": res["first_ts"],
        "last_ts": res["last_ts"],
        "total_gdd": res["total_gdd"],
        "reached": res["reached"],
        "reached_ts": res["reached_ts"],
        "gap_count": res["gap_count"],
        "complete_day_count": res["complete_day_count"],
        "forecast": res["forecast"],
    })


def build_heat_state(batch_id=None, zone_id=None):
    db = get_db()
    zones = [{"id": r["id"], "name": r["name"]}
             for r in db.execute("SELECT id,name FROM zones ORDER BY id")]
    rows = db.execute("SELECT * FROM heat_batches ORDER BY id").fetchall()
    cards = [_heat_card(db, r) for r in rows]

    # 选择优先级：显式批次 > 分区筛选出的第一个批次 > 最近批次
    selected_row = None
    if batch_id is not None:
        selected_row = next((r for r in rows if r["id"] == batch_id), None)
        if selected_row is None:
            raise KeyError("批次不存在")
    if selected_row is None and zone_id is not None:
        selected_row = next((r for r in rows if r["zone_id"] == zone_id), None)
    if selected_row is None and batch_id is None and zone_id is None and rows:
        selected_row = rows[-1]

    selected = None
    if selected_row is not None:
        zname = next((z["name"] for z in zones if z["id"] == selected_row["zone_id"]),
                     None)
        selected = _heat_batch_dict(selected_row,
                                    _heat_compute(db, selected_row))
        selected["zone_name"] = zname

    filtered = zone_id is not None
    visible = [c for c in cards if not filtered or c["zone_id"] == zone_id]
    return {
        "zones": zones,
        "filter_zone_id": zone_id,
        "batches": visible if filtered else cards,
        "all_batches": cards,
        "selected_id": selected_row["id"] if selected_row else None,
        "selected": selected,
    }


def build_heat_export(batch_id=None, zone_id=None):
    state = build_heat_state(batch_id, zone_id)
    sel = state["selected"]
    zone_by_id = {z["id"]: z["name"] for z in state["zones"]}
    return {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "filter_zone_id": state["filter_zone_id"],
        "zone": zone_by_id.get(state["filter_zone_id"]) if state["filter_zone_id"] else None,
        "zones": state["zones"],
        "batches": [
            {"id": b["id"], "zone_id": b["zone_id"],
             "zone_name": zone_by_id.get(b["zone_id"]),
             "name": b["name"], "crop": b["crop"],
             "base_temp": b["base_temp"], "cap_temp": b["cap_temp"],
             "target_gdd": b["target_gdd"], "max_gap_hours": b["max_gap_hours"],
             "summary": b["result"]}
            for b in state["all_batches"]
        ],
        "selected_id": state["selected_id"],
        "selected": sel,
    }


@app.route("/heat")
def heat_page():
    return send_from_directory(app.static_folder, "heat.html")


@app.get("/api/heat/state")
def api_heat_state():
    batch_id = request.args.get("batch_id", type=int)
    zone_id = request.args.get("zone_id", type=int)
    try:
        return jsonify(build_heat_state(batch_id, zone_id))
    except KeyError:
        return bad("批次不存在", 404)


@app.get("/api/heat/export")
def api_heat_export():
    batch_id = request.args.get("batch_id", type=int)
    zone_id = request.args.get("zone_id", type=int)
    try:
        payload = build_heat_export(batch_id, zone_id)
    except KeyError:
        return bad("批次不存在", 404)
    if payload["selected"] is None:
        return bad("没有可导出的批次")
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"crop-heat-batch-{payload['selected_id']}.json"
    resp = Response(body, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = \
        f"attachment; filename=crop-heat.json; filename*=UTF-8''{fname}"
    return resp


# ---- 批次 ----

def _read_heat_batch_payload(db, data):
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("缺少字段：批次名称")
    zone_id = data.get("zone_id")
    try:
        zone_id = int(zone_id)
    except (TypeError, ValueError):
        raise ValueError("请选择所属温室分区")
    if not db.execute("SELECT 1 FROM zones WHERE id=?", (zone_id,)).fetchone():
        raise ValueError("请选择现有温室分区")
    cfg = ch.validate_config(data)
    crop = (data.get("crop") or "").strip()
    return name, crop, zone_id, cfg


@app.post("/api/heat/batches")
def api_heat_create_batch():
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        name, crop, zone_id, cfg = _read_heat_batch_payload(db, data)
    except ValueError as e:
        return bad(str(e))
    cur = db.execute(
        "INSERT INTO heat_batches(zone_id,name,crop,base_temp,cap_temp,"
        "target_gdd,max_gap_hours,created) VALUES(?,?,?,?,?,?,?,?)",
        (zone_id, name, crop, cfg["base_temp"], cfg["cap_temp"],
         cfg["target_gdd"], cfg["max_gap_hours"],
         datetime.now().replace(microsecond=0).isoformat()))
    db.commit()
    return jsonify(build_heat_state(cur.lastrowid)), 201


@app.put("/api/heat/batches/<int:batch_id>")
def api_heat_update_batch(batch_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    if not db.execute("SELECT 1 FROM heat_batches WHERE id=?", (batch_id,)).fetchone():
        return bad("批次不存在", 404)
    try:
        name, crop, zone_id, cfg = _read_heat_batch_payload(db, data)
    except ValueError as e:
        return bad(str(e))
    db.execute(
        "UPDATE heat_batches SET zone_id=?,name=?,crop=?,base_temp=?,cap_temp=?,"
        "target_gdd=?,max_gap_hours=? WHERE id=?",
        (zone_id, name, crop, cfg["base_temp"], cfg["cap_temp"],
         cfg["target_gdd"], cfg["max_gap_hours"], batch_id))
    db.commit()
    return jsonify(build_heat_state(batch_id))


@app.delete("/api/heat/batches/<int:batch_id>")
def api_heat_delete_batch(batch_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM heat_batches WHERE id=?", (batch_id,)).fetchone():
        return bad("批次不存在", 404)
    db.execute("DELETE FROM heat_readings WHERE batch_id=?", (batch_id,))
    db.execute("DELETE FROM heat_batches WHERE id=?", (batch_id,))
    db.commit()
    return jsonify(build_heat_state())


# ---- 读数 ----

@app.post("/api/heat/batches/<int:batch_id>/readings")
def api_heat_add_readings(batch_id):
    data = request.get_json(silent=True)
    db = get_db()
    if not db.execute("SELECT 1 FROM heat_batches WHERE id=?", (batch_id,)).fetchone():
        return bad("批次不存在", 404)
    try:
        cleaned = ch.validate_readings(data)
    except ValueError as e:
        return bad(str(e))
    try:
        for row in cleaned:
            db.execute("INSERT INTO heat_readings(batch_id,ts,temp) VALUES(?,?,?)",
                       (batch_id, row["ts"], row["temp"]))
        db.commit()
    except sqlite3.IntegrityError:
        return bad("时间戳与该批次已有读数重复，请检查补录时间")
    # 补录后按时间全量重算
    return jsonify(build_heat_state(batch_id)), 201


@app.delete("/api/heat/readings/<int:reading_id>")
def api_heat_delete_reading(reading_id):
    db = get_db()
    row = db.execute("SELECT batch_id FROM heat_readings WHERE id=?",
                     (reading_id,)).fetchone()
    if not row:
        return bad("读数不存在", 404)
    db.execute("DELETE FROM heat_readings WHERE id=?", (reading_id,))
    db.commit()
    # 删除后按时间全量重算
    return jsonify(build_heat_state(row["batch_id"]))


# ---------------------------------------------------------------- API: 滴灌均匀度巡检

def _drip_zone_samples(db, dzid):
    rows = db.execute(
        "SELECT id,position,label,flow FROM drip_samples WHERE drip_zone_id=?"
        " ORDER BY position,label,id", (dzid,)).fetchall()
    return [{"id": r["id"], "drip_zone_id": dzid, "position": r["position"],
             "label": r["label"], "flow": r["flow"]} for r in rows]


def _drip_zone_payload(db, row):
    """组装单个巡检分区的重算输入（含全部样本，按首/中/尾编号）。"""
    return {
        "drip_zone_id": row["id"],
        "zone_id": row["zone_id"],
        "zone_name": row["zone_name"],
        "inlet_pressure": row["inlet_pressure"],
        "base_inlet_pressure": row["base_inlet_pressure"],
        "design_flow": row["design_flow"],
        "du_min": row["du_min"],
        "tail_drop_limit": row["tail_drop_limit"],
        "low_flow_ratio": row["low_flow_ratio"],
        "samples": _drip_zone_samples(db, row["id"]),
    }


def _drip_round_compute(db, round_row):
    """取整轮全部分区与样本整体重算。"""
    zrows = db.execute("SELECT * FROM drip_zones WHERE round_id=? ORDER BY id",
                       (round_row["id"],)).fetchall()
    payloads = [_drip_zone_payload(db, r) for r in zrows]
    result = dri.evaluate_round(payloads)
    # 结果区卡回填样本明细（供前端明细表删除 / 筛选；evaluate_round 不消费此字段）
    for zout, pin in zip(result["zones"], payloads):
        zout["samples"] = pin["samples"]
    return result


def _drip_round_dict(db, row, valve_by_id, with_result=True):
    valve = valve_by_id.get(row["valve_id"])
    out = {
        "id": row["id"],
        "name": row["name"],
        "valve_id": row["valve_id"],
        "valve_name": valve["name"] if valve else "（阀门已删除）",
        "inspected": row["inspected"],
        "note": row["note"],
        "created": row["created"],
    }
    if with_result:
        result = _drip_round_compute(db, row)
        out["result"] = result
        out["zone_count"] = result["zone_count"]
        out["sample_count"] = sum(z["sample_count"] for z in result["zones"])
    return out


def _drip_valves_zones(db):
    valves = [valve_dict(r) for r in db.execute("SELECT * FROM valves ORDER BY id")]
    valve_by_id = {v["id"]: v for v in valves}
    zones = [zone_dict(r, valve_by_id)
             for r in db.execute("SELECT * FROM zones ORDER BY id")]
    return valves, valve_by_id, zones


def build_drip_state(round_id=None, valve_id=None, day=None):
    db = get_db()
    valves, valve_by_id, zones = _drip_valves_zones(db)

    sql = "SELECT * FROM drip_rounds WHERE 1=1"
    args = []
    if valve_id is not None:
        sql += " AND valve_id=?"
        args.append(valve_id)
    if day:
        sql += " AND substr(inspected,1,10)=?"
        args.append(day)
    sql += " ORDER BY inspected DESC,id DESC"
    rows = db.execute(sql, args).fetchall()

    if round_id is None and valve_id is None and day is None and rows:
        round_id = rows[0]["id"]
    selected_row = next((r for r in rows if r["id"] == round_id), None)
    if round_id is not None and selected_row is None:
        raise KeyError("巡检轮次不存在")

    # 列表卡只带轻量汇总（整轮仍重算，保证补录后卡片计数同步）
    cards = []
    for r in rows:
        result = _drip_round_compute(db, r)
        cards.append({
            "id": r["id"], "name": r["name"],
            "valve_id": r["valve_id"],
            "valve_name": valve_by_id.get(r["valve_id"], {}).get("name", "（阀门已删除）"),
            "inspected": r["inspected"], "note": r["note"],
            "zone_count": result["zone_count"],
            "evaluated_count": result["evaluated_count"],
            "du_pass_count": result["du_pass_count"],
            "sample_count": sum(z["sample_count"] for z in result["zones"]),
            "verdict": result["verdict"],
            "verdict_label": result["verdict_label"],
        })

    selected = None
    if selected_row is not None:
        selected = _drip_round_dict(db, selected_row, valve_by_id)

    # 新建轮次时的可选分区：归属于所选阀门的分区
    available = None
    if valve_id is not None:
        available = [{"id": z["id"], "name": z["name"],
                      "valve_id": z["valve_id"], "valve_name": z["valve_name"]}
                     for z in zones if z["valve_id"] == valve_id]

    return {
        "valves": valves,
        "zones": [{"id": z["id"], "name": z["name"], "valve_id": z["valve_id"],
                   "valve_name": z["valve_name"]} for z in zones],
        "filter_valve_id": valve_id,
        "filter_date": day or "",
        "rounds": cards,
        "selected_id": selected_row["id"] if selected_row else None,
        "selected": selected,
        "available_zones": available,
    }


def build_drip_export(round_id=None, valve_id=None, day=None):
    state = build_drip_state(round_id, valve_id, day)
    if state["selected"] is not None:
        rounds = state["selected"]
    else:
        rounds = [{"id": c["id"], **c} for c in state["rounds"]]
    return {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "filter_valve_id": state["filter_valve_id"],
        "filter_date": state["filter_date"],
        "selected_id": state["selected_id"],
        "valves": state["valves"],
        "rounds": rounds,
    }


def _read_drip_round_payload(db, data):
    """校验轮次表单：名称、阀门、巡检时间、以及各分区的五个参数 + 入口压力。"""
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("缺少字段：巡检名称")
    try:
        valve_id = int(data.get("valve_id"))
    except (TypeError, ValueError):
        raise ValueError("请选择巡检阀门")
    if not db.execute("SELECT 1 FROM valves WHERE id=?", (valve_id,)).fetchone():
        raise ValueError("请选择有效阀门")
    inspected = dri.parse_ts(data.get("inspected"))
    note = (data.get("note") or "").strip()

    raw_zones = data.get("zones")
    if not isinstance(raw_zones, list) or not raw_zones:
        raise ValueError("请至少为一个阀门下分区配置巡检参数")
    bound = {r["id"] for r in db.execute(
        "SELECT id FROM zones WHERE valve_id=?", (valve_id,))}
    seen = set()
    zones = []
    for i, z in enumerate(raw_zones):
        where = f"第 {i + 1} 个分区"
        try:
            zone_id = int(z.get("zone_id"))
        except (TypeError, ValueError):
            raise ValueError(f"{where}：请选择分区")
        if zone_id not in bound:
            raise ValueError(f"{where}：分区不属于所选阀门")
        if zone_id in seen:
            raise ValueError(f"{where}：分区重复配置")
        seen.add(zone_id)
        cfg = dri.validate_config(z)
        inlet = dri.validate_inlet_pressure(z.get("inlet_pressure"), required=False)
        zones.append({"zone_id": zone_id, "inlet_pressure": inlet, **cfg})
    return name, valve_id, inspected, note, zones


def _replace_drip_zones(db, round_id, zones):
    """按提交内容整表替换轮次分区参数；被移除 / 不存在的旧分区行连同样本删除。"""
    name_by_id = {r["id"]: r["name"]
                  for r in db.execute("SELECT id,name FROM zones")}
    keep = {z["zone_id"] for z in zones}
    old_rows = db.execute("SELECT id,zone_id FROM drip_zones WHERE round_id=?",
                          (round_id,)).fetchall()
    for r in old_rows:
        if r["zone_id"] not in keep:
            db.execute("DELETE FROM drip_samples WHERE drip_zone_id=?", (r["id"],))
            db.execute("DELETE FROM drip_zones WHERE id=?", (r["id"],))
    for z in zones:
        old = db.execute(
            "SELECT id FROM drip_zones WHERE round_id=? AND zone_id=?",
            (round_id, z["zone_id"])).fetchone()
        if old:
            db.execute(
                "UPDATE drip_zones SET zone_name=?,inlet_pressure=?,"
                "base_inlet_pressure=?,design_flow=?,du_min=?,"
                "tail_drop_limit=?,low_flow_ratio=? WHERE id=?",
                (name_by_id.get(z["zone_id"], f"分区#{z['zone_id']}"),
                 z["inlet_pressure"], z["base_inlet_pressure"], z["design_flow"],
                 z["du_min"], z["tail_drop_limit"], z["low_flow_ratio"], old["id"]))
        else:
            db.execute(
                "INSERT INTO drip_zones(round_id,zone_id,zone_name,inlet_pressure,"
                "base_inlet_pressure,design_flow,du_min,tail_drop_limit,low_flow_ratio)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (round_id, z["zone_id"], name_by_id.get(z["zone_id"], "已删除分区"),
                 z["inlet_pressure"], z["base_inlet_pressure"], z["design_flow"],
                 z["du_min"], z["tail_drop_limit"], z["low_flow_ratio"]))


@app.route("/drip")
def drip_page():
    return send_from_directory(app.static_folder, "drip.html")


@app.get("/api/drip/state")
def api_drip_state():
    round_id = request.args.get("round_id", type=int)
    valve_id = request.args.get("valve_id", type=int)
    day = (request.args.get("date") or "").strip() or None
    try:
        return jsonify(build_drip_state(round_id, valve_id, day))
    except KeyError:
        return bad("巡检轮次不存在", 404)


@app.get("/api/drip/export")
def api_drip_export():
    round_id = request.args.get("round_id", type=int)
    valve_id = request.args.get("valve_id", type=int)
    day = (request.args.get("date") or "").strip() or None
    try:
        payload = build_drip_export(round_id, valve_id, day)
    except KeyError:
        return bad("巡检轮次不存在", 404)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    if round_id is not None:
        fname = f"drip-uniformity-round-{round_id}.json"
    else:
        tag = day or "all"
        fname = f"drip-uniformity-{valve_id or 'all'}-{tag}.json"
    resp = Response(body, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = \
        f"attachment; filename=drip-uniformity.json; filename*=UTF-8''{fname}"
    return resp


# ---- 轮次 ----

@app.post("/api/drip/rounds")
def api_drip_create_round():
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        name, valve_id, inspected, note, zones = _read_drip_round_payload(db, data)
    except ValueError as e:
        return bad(str(e))
    cur = db.execute(
        "INSERT INTO drip_rounds(name,valve_id,inspected,note,created) VALUES(?,?,?,?,?)",
        (name, valve_id, inspected, note,
         datetime.now().replace(microsecond=0).isoformat()))
    rid = cur.lastrowid
    _replace_drip_zones(db, rid, zones)
    db.commit()
    return jsonify(build_drip_state(rid)), 201


@app.put("/api/drip/rounds/<int:round_id>")
def api_drip_update_round(round_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    if not db.execute("SELECT 1 FROM drip_rounds WHERE id=?", (round_id,)).fetchone():
        return bad("巡检轮次不存在", 404)
    try:
        name, valve_id, inspected, note, zones = _read_drip_round_payload(db, data)
    except ValueError as e:
        return bad(str(e))
    db.execute("UPDATE drip_rounds SET name=?,valve_id=?,inspected=?,note=? WHERE id=?",
               (name, valve_id, inspected, note, round_id))
    _replace_drip_zones(db, round_id, zones)
    db.commit()
    return jsonify(build_drip_state(round_id))


@app.delete("/api/drip/rounds/<int:round_id>")
def api_drip_delete_round(round_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM drip_rounds WHERE id=?", (round_id,)).fetchone():
        return bad("巡检轮次不存在", 404)
    zrows = db.execute("SELECT id FROM drip_zones WHERE round_id=?", (round_id,)).fetchall()
    for r in zrows:
        db.execute("DELETE FROM drip_samples WHERE drip_zone_id=?", (r["id"],))
    db.execute("DELETE FROM drip_zones WHERE round_id=?", (round_id,))
    db.execute("DELETE FROM drip_rounds WHERE id=?", (round_id,))
    db.commit()
    return jsonify(build_drip_state())


# ---- 分区参数 / 入口压力（保存后整轮重算） ----

@app.put("/api/drip/zones/<int:dz_id>")
def api_drip_update_zone(dz_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM drip_zones WHERE id=?", (dz_id,)).fetchone()
    if not row:
        return bad("巡检分区不存在", 404)
    try:
        cfg = dri.validate_config(data)
        inlet = dri.validate_inlet_pressure(data.get("inlet_pressure"), required=False)
    except ValueError as e:
        return bad(str(e))
    db.execute(
        "UPDATE drip_zones SET inlet_pressure=?,base_inlet_pressure=?,"
        "design_flow=?,du_min=?,tail_drop_limit=?,low_flow_ratio=? WHERE id=?",
        (inlet, cfg["base_inlet_pressure"], cfg["design_flow"], cfg["du_min"],
         cfg["tail_drop_limit"], cfg["low_flow_ratio"], dz_id))
    db.commit()
    return jsonify(build_drip_state(row["round_id"]))


# ---- 样本录入 / 删除（补录或删除后整轮重算） ----

@app.post("/api/drip/zones/<int:dz_id>/samples")
def api_drip_add_samples(dz_id):
    data = request.get_json(silent=True)
    db = get_db()
    row = db.execute("SELECT round_id FROM drip_zones WHERE id=?", (dz_id,)).fetchone()
    if not row:
        return bad("巡检分区不存在", 404)
    try:
        cleaned = dri.validate_samples(data)
    except ValueError as e:
        return bad(str(e))
    try:
        for s in cleaned:
            db.execute(
                "INSERT INTO drip_samples(drip_zone_id,position,label,flow) VALUES(?,?,?,?)",
                (dz_id, s["position"], s["label"], s["flow"]))
        db.commit()
    except sqlite3.IntegrityError:
        return bad("同段滴头编号已存在，请检查编号（首 / 中 / 尾分别编号）")
    return jsonify(build_drip_state(row["round_id"])), 201


@app.delete("/api/drip/samples/<int:sample_id>")
def api_drip_delete_sample(sample_id):
    db = get_db()
    row = db.execute(
        "SELECT z.round_id FROM drip_samples s JOIN drip_zones z"
        " ON z.id=s.drip_zone_id WHERE s.id=?", (sample_id,)).fetchone()
    if not row:
        return bad("采样点不存在", 404)
    db.execute("DELETE FROM drip_samples WHERE id=?", (sample_id,))
    db.commit()
    return jsonify(build_drip_state(row["round_id"]))


# ---------------------------------------------------------------- API: 肥料母液分罐

def _fertilizer_items(db, recipe_id):
    return [{"id": r["id"], "name": r["name"], "amount": r["amount"]}
            for r in db.execute(
                "SELECT id,name,amount FROM fertilizer_items"
                " WHERE recipe_id=? ORDER BY id", (recipe_id,))]


def _fertilizer_pairs(db, recipe_id):
    return [{"id": r["id"], "a_id": r["a_id"], "b_id": r["b_id"]}
            for r in db.execute(
                "SELECT id,a_id,b_id FROM fertilizer_pairs"
                " WHERE recipe_id=? ORDER BY a_id,b_id", (recipe_id,))]


def _fertilizer_recipe_payload(db, row):
    """组装单份配方：取全部肥料与不相容关系整体重算分罐。"""
    zone = db.execute("SELECT name FROM zones WHERE id=?",
                      (row["zone_id"],)).fetchone() if row["zone_id"] else None
    items = _fertilizer_items(db, row["id"])
    pairs = _fertilizer_pairs(db, row["id"])
    result = fz.solve(row["cap_a"], row["cap_b"], items, pairs)
    return {
        "id": row["id"],
        "name": row["name"],
        "zone_id": row["zone_id"],
        "zone_name": zone["name"] if zone else "（分区已删除）",
        "cap_a": row["cap_a"],
        "cap_b": row["cap_b"],
        "note": row["note"],
        "created": row["created"],
        "items": items,
        "pairs": pairs,
        "result": result,
    }


def _fertilizer_card(db, row):
    """配方列表卡：带轻量状态汇总（仍整体重算，保证改用量 / 关系 / 容量后计数同步）。"""
    items = _fertilizer_items(db, row["id"])
    pairs = _fertilizer_pairs(db, row["id"])
    res = fz.solve(row["cap_a"], row["cap_b"], items, pairs)
    card = {
        "id": row["id"], "name": row["name"], "zone_id": row["zone_id"],
        "cap_a": row["cap_a"], "cap_b": row["cap_b"],
        "active_count": len(res["active"]),
        "ignored_count": len(res["ignored"]),
        "pair_count": len([e for e in res["edges"]]),
        "component_count": len(res["components"]),
        "status": res["status"], "status_label": res["status_label"],
        "total_amount": res["total_amount"],
    }
    d = res.get("decision")
    if d is not None:
        c = d["chosen"]
        card.update(used_a=c["used_a"], used_b=c["used_b"], diff=c["diff"],
                    excess_total=c["excess_total"],
                    feasible_count=d["feasible_count"])
    elif res["status"] == fz.ERROR:
        card.update(used_a=None, used_b=None, diff=None, excess_total=None,
                    feasible_count=0, error=res.get("error"))
    elif res["status"] == fz.CONFLICT:
        card.update(used_a=None, used_b=None, diff=None, excess_total=None,
                    feasible_count=0)
    else:
        card.update(used_a=0, used_b=0, diff=0, excess_total=0, feasible_count=0)
    return card


def build_fertilizer_state(recipe_id=None, zone_id=None):
    db = get_db()
    zones = [{"id": r["id"], "name": r["name"]}
             for r in db.execute("SELECT id,name FROM zones ORDER BY id")]
    sql = "SELECT * FROM fertilizer_recipes WHERE 1=1"
    args = []
    if zone_id is not None:
        sql += " AND zone_id=?"
        args.append(zone_id)
    sql += " ORDER BY id"
    rows = db.execute(sql, args).fetchall()

    if recipe_id is None and rows and zone_id is None:
        recipe_id = rows[-1]["id"]
    selected_row = next((r for r in rows if r["id"] == recipe_id), None)
    if recipe_id is not None and selected_row is None:
        raise KeyError("配方不存在")

    selected = (_fertilizer_recipe_payload(db, selected_row)
                if selected_row is not None else None)
    return {
        "zones": zones,
        "filter_zone_id": zone_id,
        "recipes": [_fertilizer_card(db, r) for r in rows],
        "selected_id": selected_row["id"] if selected_row else None,
        "selected": selected,
    }


def build_fertilizer_export(recipe_id=None, zone_id=None):
    state = build_fertilizer_state(recipe_id, zone_id)
    return {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "filter_zone_id": state["filter_zone_id"],
        "zones": state["zones"],
        "recipes": state["recipes"],
        "selected_id": state["selected_id"],
        "selected": state["selected"],
    }


def _read_recipe_payload(db, data):
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("缺少字段：配方名称")
    try:
        zone_id = int(data.get("zone_id"))
    except (TypeError, ValueError):
        raise ValueError("请选择所属温室分区")
    if not db.execute("SELECT 1 FROM zones WHERE id=?", (zone_id,)).fetchone():
        raise ValueError("请选择现有温室分区")
    cap_a = fz.validate_cap(data.get("cap_a"), "A 罐容量")
    cap_b = fz.validate_cap(data.get("cap_b"), "B 罐容量")
    note = (data.get("note") or "").strip()
    return name, zone_id, cap_a, cap_b, note


@app.route("/fertilizer")
def fertilizer_page():
    return send_from_directory(app.static_folder, "fertilizer.html")


@app.get("/api/fertilizer/state")
def api_fertilizer_state():
    recipe_id = request.args.get("recipe_id", type=int)
    zone_id = request.args.get("zone_id", type=int)
    try:
        return jsonify(build_fertilizer_state(recipe_id, zone_id))
    except KeyError:
        return bad("配方不存在", 404)


@app.get("/api/fertilizer/export")
def api_fertilizer_export():
    recipe_id = request.args.get("recipe_id", type=int)
    zone_id = request.args.get("zone_id", type=int)
    try:
        payload = build_fertilizer_export(recipe_id, zone_id)
    except KeyError:
        return bad("配方不存在", 404)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    if payload["selected_id"] is not None:
        fname = f"fertilizer-recipe-{payload['selected_id']}.json"
    else:
        fname = f"fertilizer-recipes-{zone_id or 'all'}.json"
    resp = Response(body, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = \
        f"attachment; filename=fertilizer-recipe.json; filename*=UTF-8''{fname}"
    return resp


# ---- 配方 ----

@app.post("/api/fertilizer/recipes")
def api_fertilizer_create_recipe():
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        name, zone_id, cap_a, cap_b, note = _read_recipe_payload(db, data)
    except ValueError as e:
        return bad(str(e))
    cur = db.execute(
        "INSERT INTO fertilizer_recipes(name,zone_id,cap_a,cap_b,note,created)"
        " VALUES(?,?,?,?,?,?)",
        (name, zone_id, cap_a, cap_b, note,
         datetime.now().replace(microsecond=0).isoformat()))
    db.commit()
    return jsonify(build_fertilizer_state(cur.lastrowid)), 201


@app.put("/api/fertilizer/recipes/<int:recipe_id>")
def api_fertilizer_update_recipe(recipe_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    if not db.execute("SELECT 1 FROM fertilizer_recipes WHERE id=?",
                      (recipe_id,)).fetchone():
        return bad("配方不存在", 404)
    try:
        name, zone_id, cap_a, cap_b, note = _read_recipe_payload(db, data)
    except ValueError as e:
        return bad(str(e))
    db.execute(
        "UPDATE fertilizer_recipes SET name=?,zone_id=?,cap_a=?,cap_b=?,note=?"
        " WHERE id=?", (name, zone_id, cap_a, cap_b, note, recipe_id))
    db.commit()
    # 容量变更后立即重算
    return jsonify(build_fertilizer_state(recipe_id))


@app.delete("/api/fertilizer/recipes/<int:recipe_id>")
def api_fertilizer_delete_recipe(recipe_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM fertilizer_recipes WHERE id=?",
                      (recipe_id,)).fetchone():
        return bad("配方不存在", 404)
    db.execute("DELETE FROM fertilizer_pairs WHERE recipe_id=?", (recipe_id,))
    db.execute("DELETE FROM fertilizer_items WHERE recipe_id=?", (recipe_id,))
    db.execute("DELETE FROM fertilizer_recipes WHERE id=?", (recipe_id,))
    db.commit()
    return jsonify(build_fertilizer_state())


# ---- 肥料 ----

@app.post("/api/fertilizer/recipes/<int:recipe_id>/items")
def api_fertilizer_create_item(recipe_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    if not db.execute("SELECT 1 FROM fertilizer_recipes WHERE id=?",
                      (recipe_id,)).fetchone():
        return bad("配方不存在", 404)
    name = (data.get("name") or "").strip()
    if not name:
        return bad("缺少字段：肥料名称")
    try:
        amount = fz.validate_amount(data.get("amount"), "所需母液量")
    except ValueError as e:
        return bad(str(e))
    try:
        db.execute(
            "INSERT INTO fertilizer_items(recipe_id,name,amount) VALUES(?,?,?)",
            (recipe_id, name, amount))
        db.commit()
    except sqlite3.IntegrityError:
        return bad("该配方下肥料名称已存在")
    # 用量变更（新增）后立即重算
    return jsonify(build_fertilizer_state(recipe_id)), 201


@app.put("/api/fertilizer/items/<int:item_id>")
def api_fertilizer_update_item(item_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM fertilizer_items WHERE id=?",
                     (item_id,)).fetchone()
    if not row:
        return bad("肥料不存在", 404)
    name = row["name"]
    amount = row["amount"]
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return bad("缺少字段：肥料名称")
    # 用量 0 是合法值（表示暂不参与求解），不能用真值判断跳过
    if data.get("amount") is not None:
        try:
            amount = fz.validate_amount(data.get("amount"), "所需母液量")
        except ValueError as e:
            return bad(str(e))
    try:
        db.execute("UPDATE fertilizer_items SET name=?,amount=? WHERE id=?",
                   (name, amount, item_id))
        db.commit()
    except sqlite3.IntegrityError:
        return bad("该配方下肥料名称已存在")
    # 编辑名称 / 用量后立即重算
    return jsonify(build_fertilizer_state(row["recipe_id"]))


@app.delete("/api/fertilizer/items/<int:item_id>")
def api_fertilizer_delete_item(item_id):
    db = get_db()
    row = db.execute("SELECT recipe_id FROM fertilizer_items WHERE id=?",
                     (item_id,)).fetchone()
    if not row:
        return bad("肥料不存在", 404)
    db.execute("DELETE FROM fertilizer_pairs WHERE a_id=? OR b_id=?",
               (item_id, item_id))
    db.execute("DELETE FROM fertilizer_items WHERE id=?", (item_id,))
    db.commit()
    return jsonify(build_fertilizer_state(row["recipe_id"]))


# ---- 不相容关系 ----

def _resolve_pair_items(db, recipe_id, data):
    try:
        a_id = int(data.get("a_id"))
        b_id = int(data.get("b_id"))
    except (TypeError, ValueError):
        raise ValueError("请选择两种肥料")
    if a_id == b_id:
        raise ValueError("不相容关系必须选择两种不同的肥料")
    rows = db.execute(
        "SELECT id FROM fertilizer_items WHERE recipe_id=? AND id IN (?,?)",
        (recipe_id, a_id, b_id)).fetchall()
    if len(rows) != 2:
        raise ValueError("请选择本配方内的肥料")
    return (a_id, b_id) if a_id < b_id else (b_id, a_id)


@app.post("/api/fertilizer/recipes/<int:recipe_id>/pairs")
def api_fertilizer_create_pair(recipe_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    if not db.execute("SELECT 1 FROM fertilizer_recipes WHERE id=?",
                      (recipe_id,)).fetchone():
        return bad("配方不存在", 404)
    try:
        a_id, b_id = _resolve_pair_items(db, recipe_id, data)
    except ValueError as e:
        return bad(str(e))
    try:
        db.execute(
            "INSERT INTO fertilizer_pairs(recipe_id,a_id,b_id) VALUES(?,?,?)",
            (recipe_id, a_id, b_id))
        db.commit()
    except sqlite3.IntegrityError:
        return bad("这两种肥料的不相容关系已存在")
    # 关系变更后立即重算
    return jsonify(build_fertilizer_state(recipe_id)), 201


@app.delete("/api/fertilizer/pairs/<int:pair_id>")
def api_fertilizer_delete_pair(pair_id):
    db = get_db()
    row = db.execute("SELECT recipe_id FROM fertilizer_pairs WHERE id=?",
                     (pair_id,)).fetchone()
    if not row:
        return bad("不相容关系不存在", 404)
    db.execute("DELETE FROM fertilizer_pairs WHERE id=?", (pair_id,))
    db.commit()
    return jsonify(build_fertilizer_state(row["recipe_id"]))


# ---------------------------------------------------------------- API: 执行核验

def _verify_event_dict(row):
    return {
        "id": row["id"], "event_id": row["event_id"],
        "batch_id": row["batch_id"], "valve_id": row["valve_id"],
        "start_min": row["start_min"], "end_min": row["end_min"],
        "flow_lmin": row["flow_lmin"], "created": row["created"],
    }


def _verify_batch_row(db, row, with_result=True):
    """组装批次：with_result=True 时按当前计划全量重算。"""
    valve_rows = db.execute("SELECT * FROM valves ORDER BY id").fetchall()
    valves = [valve_dict(r) for r in valve_rows]
    valve_by_id = {v["id"]: v for v in valves}
    zones = [zone_dict(r, valve_by_id)
             for r in db.execute("SELECT * FROM zones ORDER BY id")]
    items = [item_dict(r)
             for r in db.execute(
                 "SELECT * FROM schedule_items ORDER BY start_slot,zone_id")]
    blackouts = [blackout_dict(r)
                 for r in db.execute("SELECT * FROM blackouts ORDER BY id")]
    settings = {r["key"]: r["value"]
                for r in db.execute("SELECT key,value FROM settings")}
    total_flow = float(settings.get("total_flow", 100))
    day = ec.parse_day(row["plan_date"])
    events = [_verify_event_dict(r) for r in db.execute(
        "SELECT * FROM verify_events WHERE batch_id=? ORDER BY start_min,event_id",
        (row["id"],))]
    result = ec.check_day(zones, valves, items, blackouts, events, day,
                          total_flow) if with_result else None
    if result is not None:
        # 把事件行主键回填到重算结果，供前端逐条删除（删除后全量重算）
        pk_by_key = {(e["event_id"], e["start_min"], e["end_min"],
                      ec.r2(e["flow_lmin"])): e["id"] for e in events}
        for d in result["events"]:
            d["id"] = pk_by_key.get((d["event_id"], d["start_min"],
                                     d["end_min"], ec.r2(d["flow_lmin"])))
    return {
        "id": row["id"], "name": row["name"], "plan_date": row["plan_date"],
        "note": row["note"], "created": row["created"],
        "plan_revision": row["plan_revision"],
        "current_revision": int(settings.get("plan_revision", 1)),
        "event_count": len(events),
        "result": result,
    }


def build_verify_state(batch_id=None, day=None):
    db = get_db()
    sql = "SELECT * FROM verify_batches WHERE 1=1"
    args = []
    if day:
        sql += " AND plan_date=?"
        args.append(day)
    sql += " ORDER BY plan_date,id"
    rows = db.execute(sql, args).fetchall()

    if batch_id is None and day is None and rows:
        batch_id = rows[-1]["id"]
    selected_row = next((r for r in rows if r["id"] == batch_id), None)
    if batch_id is not None and selected_row is None:
        raise KeyError("核验批次不存在")

    # 卡片只带轻量计数（不重算）
    cards = []
    for r in rows:
        cnt = db.execute(
            "SELECT COUNT(*) c FROM verify_events WHERE batch_id=?",
            (r["id"],)).fetchone()["c"]
        cards.append({
            "id": r["id"], "name": r["name"], "plan_date": r["plan_date"],
            "note": r["note"], "created": r["created"],
            "plan_revision": r["plan_revision"], "event_count": cnt,
        })

    selected = _verify_batch_row(db, selected_row) if selected_row is not None \
        else None

    plan_date = db.execute(
        "SELECT value FROM settings WHERE key='plan_date'").fetchone()
    valves = [valve_dict(r) for r in db.execute("SELECT * FROM valves ORDER BY id")]
    return {
        "plan_date": plan_date["value"] if plan_date else "",
        "filter_date": day or "",
        "valves": valves,
        "batches": cards,
        "selected_id": selected_row["id"] if selected_row else None,
        "selected": selected,
    }


def build_verify_export(batch_id):
    state = build_verify_state(batch_id)
    sel = state["selected"]
    if sel is None:
        raise KeyError("核验批次不存在")
    return {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "batch": {k: sel[k] for k in
                  ("id", "name", "plan_date", "note", "created",
                   "plan_revision", "current_revision")},
        "result": sel["result"],
    }


@app.route("/verify")
def verify_page():
    return send_from_directory(app.static_folder, "verify.html")


@app.get("/api/verify/state")
def api_verify_state():
    batch_id = request.args.get("batch_id", type=int)
    day = (request.args.get("date") or "").strip() or None
    try:
        return jsonify(build_verify_state(batch_id, day))
    except KeyError:
        return bad("核验批次不存在", 404)
    except ValueError as e:
        return bad(str(e))


@app.get("/api/verify/export")
def api_verify_export():
    batch_id = request.args.get("batch_id", type=int)
    if not batch_id:
        return bad("请指定核验批次")
    try:
        payload = build_verify_export(batch_id)
    except KeyError:
        return bad("核验批次不存在", 404)
    except ValueError as e:
        return bad(str(e))
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"execution-verify-{batch_id}-{payload['batch']['plan_date']}.json"
    resp = Response(body, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = \
        f"attachment; filename=execution-verify.json; filename*=UTF-8''{fname}"
    return resp


@app.post("/api/verify/batches")
def api_verify_create_batch():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return bad("缺少字段：批次名称")
    raw_day = (data.get("plan_date") or "").strip()
    db = get_db()
    settings = {r["key"]: r["value"]
                for r in db.execute("SELECT key,value FROM settings")}
    raw_day = raw_day or settings.get("plan_date") or date.today().isoformat()
    try:
        ec.parse_day(raw_day)
    except ValueError as e:
        return bad(str(e))
    now = datetime.now().replace(microsecond=0).isoformat()
    cur = db.execute(
        "INSERT INTO verify_batches(name,plan_date,note,plan_revision,created)"
        " VALUES(?,?,?,?,?)",
        (name, raw_day, (data.get("note") or "").strip(),
         int(settings.get("plan_revision", 1)), now))
    db.commit()
    return jsonify(build_verify_state(cur.lastrowid)), 201


@app.delete("/api/verify/batches/<int:batch_id>")
def api_verify_delete_batch(batch_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM verify_batches WHERE id=?",
                      (batch_id,)).fetchone():
        return bad("核验批次不存在", 404)
    db.execute("DELETE FROM verify_events WHERE batch_id=?", (batch_id,))
    db.execute("DELETE FROM verify_batches WHERE id=?", (batch_id,))
    db.commit()
    return jsonify(build_verify_state())


@app.post("/api/verify/batches/<int:batch_id>/events")
def api_verify_import_events(batch_id):
    """按时间递增导入阀门运行事件；同编号同内容幂等，内容冲突整批拒绝。"""
    data = request.get_json(silent=True)
    db = get_db()
    brow = db.execute("SELECT * FROM verify_batches WHERE id=?",
                      (batch_id,)).fetchone()
    if not brow:
        return bad("核验批次不存在", 404)
    if not isinstance(data, dict):
        return bad("请求体必须为 JSON 对象")
    raw_events = data.get("events")
    if raw_events is None and isinstance(data.get("batch"), list):
        raw_events = data["batch"]
    if not isinstance(raw_events, list):
        return bad("events 必须为数组")
    try:
        day = ec.parse_day(brow["plan_date"])
        valve_ids = {r["id"] for r in db.execute("SELECT id FROM valves")}
        cleaned = ec.validate_events(raw_events, day, valve_ids)
        existing = [dict(event_id=r["event_id"], valve_id=r["valve_id"],
                         start_min=r["start_min"], end_min=r["end_min"],
                         flow_lmin=r["flow_lmin"])
                    for r in db.execute(
                        "SELECT * FROM verify_events WHERE batch_id=?",
                        (batch_id,))]
        merged, inserted, skipped = ec.merge_with_existing(cleaned, existing)
    except ValueError as e:
        # 校验 / 冲突：不落库任何一条，整批拒绝
        return bad(str(e))
    try:
        db.execute("BEGIN IMMEDIATE")
        now = datetime.now().replace(microsecond=0).isoformat()
        # 锁内重新读取，捕获「校验后、提交前」其他请求插入的同编号事件
        current_ids = {r["event_id"] for r in db.execute(
            "SELECT event_id FROM verify_events WHERE batch_id=?", (batch_id,))}
        for e in merged:
            if e["event_id"] in current_ids:
                continue
            db.execute(
                "INSERT INTO verify_events(batch_id,event_id,valve_id,"
                "start_min,end_min,flow_lmin,created) VALUES(?,?,?,?,?,?,?)",
                (batch_id, e["event_id"], e["valve_id"], e["start_min"],
                 e["end_min"], e["flow_lmin"], now))
        db.execute("COMMIT")
    except sqlite3.IntegrityError:
        db.execute("ROLLBACK")
        return bad("event_id 重复（并发提交），请刷新后重试", 409)
    except Exception:
        db.execute("ROLLBACK")
        raise
    payload = build_verify_state(batch_id)
    payload["imported"] = inserted
    payload["skipped"] = skipped
    return jsonify(payload), 201 if inserted else 200


@app.delete("/api/verify/events/<int:event_pk>")
def api_verify_delete_event(event_pk):
    db = get_db()
    row = db.execute("SELECT batch_id FROM verify_events WHERE id=?",
                     (event_pk,)).fetchone()
    if not row:
        return bad("事件不存在", 404)
    db.execute("DELETE FROM verify_events WHERE id=?", (event_pk,))
    db.commit()
    # 删除后全量重算
    return jsonify(build_verify_state(row["batch_id"]))


# ---------------------------------------------------------------- 错误

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "接口不存在"}), 404
    return "Not Found", 404


@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": "请求格式错误"}), 400


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
