#!/usr/bin/env python3
"""Public Transport Delay & Reliability Analyzer — dependency-free local app."""
from __future__ import annotations

import csv
import io
import json
import math
import os
import random
import secrets
import sqlite3
import statistics
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).parent
SESSION_TTL = 12 * 60 * 60
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
SESSIONS: dict[str, dict] = {}
SESSION_LOCK = threading.RLock()
DEMO_DATA: list[dict] | None = None
STORE = None


class SessionStore:
    """Persist visitor uploads locally or in Postgres when DATABASE_URL is set."""

    def __init__(self):
        self.database_url = os.environ.get("DATABASE_URL", "").strip()
        if self.database_url:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError("DATABASE_URL is set, but psycopg is not installed.") from exc
            self._connect = lambda: psycopg.connect(self.database_url.replace("postgres://", "postgresql://", 1))
            self._placeholder = "%s"
        else:
            path = Path(os.environ.get("TRANSIT_DB_PATH", ROOT / "transit.sqlite3"))
            path.parent.mkdir(parents=True, exist_ok=True)
            self._connect = lambda: sqlite3.connect(path, timeout=10)
            self._placeholder = "?"
        self._initialize()

    def _initialize(self):
        with closing(self._connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS transit_sessions (
                session_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                last_seen DOUBLE PRECISION NOT NULL
            )""")

    def load(self, session_id: str, now: float):
        p = self._placeholder
        with closing(self._connect()) as connection, connection:
            connection.execute(f"DELETE FROM transit_sessions WHERE last_seen < {p}", (now - SESSION_TTL,))
            row = connection.execute(
                f"SELECT payload FROM transit_sessions WHERE session_id = {p} AND last_seen >= {p}",
                (session_id, now - SESSION_TTL),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row[0])
            connection.execute(f"UPDATE transit_sessions SET last_seen = {p} WHERE session_id = {p}", (now, session_id))
            return payload

    def save(self, session_id: str, state: dict, now: float):
        p = self._placeholder
        payload = json.dumps({"data": state["data"], "audit": state["audit"]}, separators=(",", ":"))
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"""INSERT INTO transit_sessions(session_id, payload, last_seen) VALUES ({p}, {p}, {p})
                ON CONFLICT(session_id) DO UPDATE SET payload = excluded.payload, last_seen = excluded.last_seen""",
                (session_id, payload, now),
            )

    def delete(self, session_id: str):
        with closing(self._connect()) as connection, connection:
            connection.execute(f"DELETE FROM transit_sessions WHERE session_id = {self._placeholder}", (session_id,))


def session_store() -> SessionStore:
    global STORE
    if STORE is None:
        STORE = SessionStore()
    return STORE


def demo_state() -> dict:
    global DEMO_DATA
    if DEMO_DATA is None:
        DEMO_DATA = make_demo()
    return {"data": DEMO_DATA, "audit": {"input": len(DEMO_DATA), "valid": len(DEMO_DATA), "rejected": 0,
            "duplicates": 0, "retention": 100, "imputed": 0, "source": "Generated sample"}}


def make_demo() -> list[dict]:
    rng = random.Random(42)
    routes = [("BUS-101", "Downtown Express", "Bus", .85), ("BUS-204", "Cross-Town Shuttle", "Bus", .7),
              ("METRO-M1", "Red Line", "Metro", .08), ("TRAM-T3", "Riverside Line", "Tram", .35),
              ("RAIL-C2", "Regional South", "Rail", .12)]
    start = datetime(2026, 9, 1)
    rows = []
    for day in range(28):
        for route, name, mode, exposure in routes:
            for trip in range(26):
                hour = rng.choice([6, 7, 8, 9, 12, 15, 16, 17, 18, 20])
                rush = (hour in (7, 8, 9, 16, 17, 18))
                delay = max(-4, rng.gauss(1.0 + exposure * (3.2 if rush else .8), 1.3 + exposure * 2))
                if rng.random() < .025: delay += rng.uniform(8, 22)
                sched = start + timedelta(days=day, hours=hour, minutes=trip*2)
                rows.append({"route_id": route, "route_name": name, "mode": mode, "trip_id": f"{route}-{day}-{trip}",
                             "scheduled_time": sched.isoformat(), "actual_time": (sched+timedelta(minutes=delay)).isoformat(),
                             "delay_min": round(delay, 2), "headway_min": round(max(3, rng.gauss(12 if mode == "Bus" else 8, 2)), 2),
                             "weather": rng.choice(["Clear", "Clear", "Clear", "Rain"]), "hour": hour})
    return rows


def clean_csv(raw: str) -> tuple[list[dict], dict]:
    reader = csv.DictReader(io.StringIO(raw.lstrip("\ufeff")))
    if not reader.fieldnames: raise ValueError("The CSV has no header row.")
    aliases = {"route_id": ["route_id", "route", "line", "routeid"], "trip_id": ["trip_id", "trip", "vehicle_trip"],
               "scheduled_time": ["scheduled_time", "scheduled", "scheduled_arrival", "sched_time"],
               "actual_time": ["actual_time", "actual", "actual_arrival", "arrival_time"],
               "delay_min": ["delay_min", "delay_minutes", "delay", "delay_mins"],
               "route_name": ["route_name", "name"], "mode": ["mode", "transport_mode"], "headway_min": ["headway_min", "headway"]}
    names = {str(n).strip().lower().replace(" ", "_"): n for n in reader.fieldnames}
    cols = {key: next((names[a] for a in options if a in names), None) for key, options in aliases.items()}
    if not cols["delay_min"] and not (cols["scheduled_time"] and cols["actual_time"]):
        raise ValueError("CSV needs delay_min, or both scheduled_time and actual_time columns. Optional: route_id, trip_id, mode, headway_min.")
    rows, rejected, seen, duplicates, imputed = [], 0, set(), 0, 0
    for source in reader:
        get = lambda k: (source.get(cols[k], "") or "").strip() if cols[k] else ""
        route = get("route_id") or "UNSPECIFIED"
        trip = get("trip_id")
        key = (trip, get("scheduled_time")) if trip else None
        if key and key in seen: duplicates += 1; continue
        if key: seen.add(key)
        try:
            if get("delay_min"):
                delay = float(get("delay_min"))
            else:
                a, b = datetime.fromisoformat(get("actual_time").replace("Z", "+00:00")), datetime.fromisoformat(get("scheduled_time").replace("Z", "+00:00"))
                delay = (a-b).total_seconds()/60
            if not math.isfinite(delay) or delay < -30 or delay > 180: raise ValueError
            hour = 12
            stamp = get("scheduled_time")
            if stamp:
                try: hour = datetime.fromisoformat(stamp.replace("Z", "+00:00")).hour
                except ValueError: pass
            rows.append({"route_id": route.upper().replace("_", "-"), "route_name": get("route_name") or route,
                         "mode": get("mode") or "Unknown", "trip_id": trip, "delay_min": delay,
                         "headway_min": float(get("headway_min") or 0), "hour": hour, "weather": source.get("weather", "Unknown"),
                         "scheduled_time": stamp, "service_date": stamp[:10] if len(stamp) >= 10 else ""})
        except (ValueError, TypeError): rejected += 1
    if not rows: raise ValueError("No valid records found. Check timestamps and delay values.")
    return rows, {"input": sum(1 for _ in []) + len(rows) + rejected + duplicates, "valid": len(rows), "rejected": rejected, "duplicates": duplicates, "retention": round(100*len(rows)/max(1,len(rows)+rejected+duplicates),1), "imputed": imputed}


def percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    if len(s) == 1: return s[0]
    pos = (len(s)-1)*p; lo = int(pos); hi = min(lo+1,len(s)-1)
    return s[lo] + (s[hi]-s[lo])*(pos-lo)


def analytics(rows: list[dict], audit: dict | None = None) -> dict:
    audit = audit or {}
    if not rows: return {"kpis":{"trips":0,"routes":0,"otp":0,"mean":0,"p95":0,"bti":0,"ewt":0},"routes":[],"hourly":[{"hour":h,"delay":0,"trips":0} for h in range(24)],"forecast":[{"step":i+1,"delay":0} for i in range(48)],"audit":audit}
    delays = [float(r["delay_min"]) for r in rows]
    mean = statistics.mean(delays); p95 = percentile(delays, .95)
    routes = []
    byroute = {}
    for r in rows: byroute.setdefault(r["route_id"], []).append(r)
    for rid, rr in sorted(byroute.items()):
        ds = [float(r["delay_min"]) for r in rr]; avg = statistics.mean(ds); p = percentile(ds,.95)
        otp = sum(-1 <= d <= 5 for d in ds)/len(ds)*100
        bti = (p-avg)/max(avg,1)
        routes.append({"route_id":rid,"name":rr[0]["route_name"],"mode":rr[0]["mode"],"trips":len(ds),"otp":round(otp,1),"mean":round(avg,2),"p95":round(p,2),"bti":round(bti,2),"grade":"A+" if otp>=93 else "A" if otp>=88 else "B" if otp>=80 else "C"})
    byhour={i:[] for i in range(24)}
    for r in rows: byhour[int(r.get("hour",12))%24].append(float(r["delay_min"]))
    hourly=[{"hour":h,"delay":round(statistics.mean(v),2) if v else 0,"trips":len(v)} for h,v in byhour.items()]
    ewt=0
    headways=[float(r.get("headway_min",0)) for r in rows if float(r.get("headway_min",0))>0]
    if headways:
        scheduled=statistics.mean(headways)
        actual=statistics.mean([h*h for h in headways])/(2*scheduled)
        ewt=max(0, actual-scheduled/2)
    return {"kpis":{"trips":len(rows),"routes":len(routes),"otp":round(sum(-1<=d<=5 for d in delays)/len(delays)*100,1),"mean":round(mean,2),"p95":round(p95,2),"bti":round((p95-mean)/max(mean,1),2),"ewt":round(ewt,2)},"routes":routes,"hourly":hourly,"forecast":[{"step":i+1,"delay":round(max(0, (hourly[(datetime.now().hour+i)%24]["delay"]*.55 + statistics.mean([h["delay"] for h in hourly])*.45) + math.sin((datetime.now().hour+i)*math.pi/12)*.5),2)} for i in range(48)],"audit":audit}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def get_session(self):
        if hasattr(self, "session_state"):
            return self.session_state
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        session_id = jar.get("transit_session").value if jar.get("transit_session") else ""
        now = time.time()
        with SESSION_LOCK:
            expired = [sid for sid, state in SESSIONS.items() if now-state["last_seen"] > SESSION_TTL]
            for sid in expired:
                SESSIONS.pop(sid, None)
            if session_id not in SESSIONS:
                state = session_store().load(session_id, now) if session_id else None
                if state is None:
                    session_id = secrets.token_urlsafe(32)
                    state = demo_state()
                    self.new_session_cookie = session_id
                state["last_seen"] = now
                SESSIONS[session_id] = state
            else:
                SESSIONS[session_id]["last_seen"] = now
            self.session_state = SESSIONS[session_id]
            self.session_id = session_id
        return self.session_state

    def send(self, code, body, typ="application/json"):
        b=body if isinstance(body,bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type",typ)
        self.send_header("Content-Length",str(len(b)))
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Cache-Control","no-store")
        if getattr(self,"new_session_cookie",None):
            cookie=f"transit_session={self.new_session_cookie}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax"
            if os.environ.get("COOKIE_SECURE","0").lower() in ("1","true","yes"):
                cookie += "; Secure"
            self.send_header("Set-Cookie",cookie)
        self.end_headers()
        try: self.wfile.write(b)
        except (BrokenPipeError,ConnectionResetError): pass

    def read_json(self):
        length=int(self.headers.get("Content-Length",0))
        if length<=0: raise ValueError("Request body is empty.")
        if length>MAX_UPLOAD_BYTES: raise ValueError("Request is too large (5 MB maximum).")
        return json.loads(self.rfile.read(length))

    def filtered(self, params):
        rows=self.get_session()["data"]
        route=params.get("route",[""])[0]; mode=params.get("mode",[""])[0]; weather=params.get("weather",[""])[0]
        start=params.get("start",[""])[0]; end=params.get("end",[""])[0]
        traffic=float(params.get("traffic",["0"])[0] or 0); rain=float(params.get("rain",["0"])[0] or 0)
        result=[]
        for r in rows:
            if route and r["route_id"]!=route: continue
            if mode and r["mode"].lower()!=mode.lower(): continue
            if weather and r.get("weather","").lower()!=weather.lower(): continue
            day=r.get("service_date","") or str(r.get("scheduled_time",""))[:10]
            if start and (not day or day<start): continue
            if end and (not day or day>end): continue
            row=dict(r); row["observed_delay_min"]=float(row["delay_min"]); exposure=0.85 if row["mode"].lower()=="bus" else 0.35 if row["mode"].lower()=="tram" else 0.08 if row["mode"].lower()=="metro" else 0.12
            row["delay_min"]=float(row["delay_min"])+traffic/100*8*exposure+rain/100*3*exposure
            result.append(row)
        return result
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/healthz": self.send(200,json.dumps({"status":"ok"})); return
        if path=="/": self.get_session(); self.send(200,(ROOT/"index.html").read_bytes(),"text/html; charset=utf-8"); return
        if path=="/style.css": self.send(200,(ROOT/"style.css").read_bytes(),"text/css; charset=utf-8"); return
        if path=="/app.js": self.send(200,(ROOT/"app.js").read_bytes(),"text/javascript; charset=utf-8"); return
        if path=="/api/analytics":
            state=self.get_session(); data=self.filtered(parse_qs(urlparse(self.path).query)); self.send(200,json.dumps(analytics(data,state["audit"]))); return
        if path=="/api/data": self.get_session(); self.send(200,json.dumps(self.filtered(parse_qs(urlparse(self.path).query)))); return
        if path=="/api/sample":
            state=self.get_session(); session_store().delete(self.session_id); state.update(demo_state()); self.send(200,json.dumps(analytics(state["data"],state["audit"]))); return
        self.send(404,"{}")

    def do_POST(self):
        path=urlparse(self.path).path
        state=self.get_session()
        if path=="/api/upload":
            try:
                payload=self.read_json()
                rows,audit=clean_csv(payload.get("csv","")); audit["source"]=str(payload.get("name","Uploaded CSV"))[:160]
                session_store().save(self.session_id, {"data": rows, "audit": audit}, time.time())
                with SESSION_LOCK: state["data"],state["audit"]=rows,audit
                self.send(200,json.dumps(analytics(rows,audit)))
            except Exception as e: self.send(400,json.dumps({"error":str(e)}))
        elif path=="/api/ask":
            try:
                payload=self.read_json()
                q=payload.get("question","").lower().strip(); a=analytics(self.filtered({k:[str(v)] for k,v in payload.get("filters",{}).items()}),state["audit"]); k=a["kpis"]
                if not q: answer="Type a question about the loaded transit data."
                elif not k["trips"]: answer="No trips match the current filters. Reset or adjust the filters and try again."
                elif any(w in q for w in ("worst","unreliable","lowest","bad")):
                    r=min(a["routes"],key=lambda x:x["otp"]); answer=f"{r['route_id']} ({r['name']}) has the lowest on-time performance at {r['otp']}%, with a mean delay of {r['mean']} minutes and a 95th-percentile delay of {r['p95']} minutes."
                elif any(w in q for w in ("best","reliable","highest","top")):
                    r=max(a["routes"],key=lambda x:x["otp"]); answer=f"{r['route_id']} ({r['name']}) has the highest on-time performance at {r['otp']}%, based on {r['trips']:,} trips."
                elif "forecast" in q or "predict" in q: answer=f"The 48-hour forecast starts at {a['forecast'][0]['delay']} minutes average delay and ends at {a['forecast'][-1]['delay']} minutes. It is a seasonal baseline estimate, not a live service prediction."
                elif "bti" in q or "buffer" in q: answer=f"Network Buffer Time Index is {k['bti']}. The mean delay is {k['mean']} minutes and the 95th-percentile delay is {k['p95']} minutes. Higher BTI means more buffer is needed to cover delay variability."
                elif "wait" in q or "bunch" in q: answer=f"Estimated Excess Wait Time is {k['ewt']} minutes{'' if k['ewt'] else ' (headway data is missing or insufficient)'}."
                elif "otp" in q or "on time" in q or "punctual" in q: answer=f"Network on-time performance is {k['otp']}%, using the report's window of 1 minute early through 5 minutes late."
                elif "compare" in q and ("bus" in q or "metro" in q):
                    buckets={}
                    for row in self.filtered({k:[str(v)] for k,v in payload.get("filters",{}).items()}): buckets.setdefault(row["mode"],[]).append(float(row["delay_min"]))
                    parts=[]
                    for m,v in buckets.items():
                        if m.lower() in ("bus","metro"):
                            otp=100*sum(-1<=x<=5 for x in v)/len(v)
                            parts.append(f"{m}: {otp:.1f}% on-time; {statistics.mean(v):.2f} min average delay across {len(v)} trips")
                    answer="Mode comparison — "+("; ".join(parts) if parts else "no bus or metro rows match the current filters.")
                elif "rain" in q or "weather" in q:
                    rows=self.filtered({k:[str(v)] for k,v in payload.get("filters",{}).items()}); buckets={}
                    for row in rows:
                        w=row.get("weather", "Unknown").lower(); buckets.setdefault(w,[]).append(float(row["delay_min"]))
                    answer="Observed average delays by weather: "+("; ".join(f"{w}: {statistics.mean(v):.2f} min ({len(v)} trips)" for w,v in buckets.items()) if buckets else "no matching rows.") + " This is an association in the uploaded data, not proof that weather caused the delays."
                elif "route" in q: answer=f"There are {k['routes']} routes in this data. " + "; ".join(f"{r['route_id']}: {r['otp']}% OTP" for r in sorted(a["routes"],key=lambda x:-x["otp"]))
                else: answer=f"For {k['trips']:,} valid trips across {k['routes']} routes, network OTP is {k['otp']}%, mean delay is {k['mean']} minutes, and 95th-percentile delay is {k['p95']} minutes. Try asking which route is best or worst, the OTP, buffer index, wait time, or forecast."
                self.send(200,json.dumps({"answer":answer}))
            except Exception as e: self.send(400,json.dumps({"error":str(e)}))
        else: self.send(404,"{}")

if __name__=="__main__":
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    print(f"Transit Reliability Analyzer listening on {host}:{port}")
    ThreadingHTTPServer((host,port),Handler).serve_forever()
