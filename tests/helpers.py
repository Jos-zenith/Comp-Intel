"""Shared helpers for the workflow tests (fixture lives in conftest.py)."""
import main
from workflow import db as wdb


def token(c):
    with c.session_transaction() as s:
        return s["csrf_token"]


def post(c, url, data=None, **kw):
    data = dict(data or {})
    data.setdefault("csrf_token", token(c))
    return c.post(url, data=data, **kw)


def users(c):
    with main.app.app_context():
        return {r["role"]: r["id"] for r in wdb.query("SELECT id, role FROM users ORDER BY id DESC")}


def as_role(c, role):
    r = post(c, "/app/switch-user", {"user_id": users(c)[role]})
    assert r.status_code == 302


def as_user(c, user_id):
    assert post(c, "/app/switch-user", {"user_id": user_id}).status_code == 302


def q(sql, params=()):
    with main.app.app_context():
        return [dict(r) for r in wdb.query(sql, params)]


def run_sql(sql, params=()):
    with main.app.app_context():
        wdb.execute(sql, params)
        wdb.commit()


def demo(c):
    as_role(c, "admin")
    assert post(c, "/app/company/demo").status_code == 302
    as_role(c, "recruiter")


def new_offer(c, requisition_id=1, years=6, **extra):
    data = {"requisition_id": requisition_id, "candidate_name": "Test Candidate", "years_experience": years,
            "skill_category": "Core", "education_pedigree": "Recognised university", **extra}
    r = post(c, "/app/offers/new", data)
    assert r.status_code == 302, r.data[:500]
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def benchmark(rid):
    return wdb.loads(q("SELECT benchmark FROM offer_requests WHERE id = ?", (rid,))[0]["benchmark"])


def package_form(bm, cash=None, variable_share=0.1, **extra):
    if cash is None:
        cash = bm["target"] if bm["display"]["show_target"] else 20.0
    variable = round(cash * variable_share, 2)
    return {"label": "A", "fixed_pay": round(cash - variable, 2), "variable_pay": variable,
            "esop_options": bm["esop"]["options_mid"], "vesting_years": 4, "cliff_months": 12, **extra}


def submit(c, rid, form):
    return post(c, f"/app/offers/{rid}/package", {**form, "action": "submit"})


def approve_all(c, rid):
    route = [s["role"] for s in wdb.loads(q("SELECT route FROM submissions WHERE request_id = ? ORDER BY revision DESC",
                                            (rid,))[0]["route"])]
    for role in route:
        as_role(c, role)
        assert post(c, f"/app/offers/{rid}/decision", {"action": "approve", "comment": "ok"}).status_code == 302
    as_role(c, "recruiter")
    return route


def candidate_doc(html):
    start = html.index('<article class="candidate-doc')
    return html[html.index(">", start) + 1:html.index("</article>")]
