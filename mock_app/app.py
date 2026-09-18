"""
MockBank - a fake internal bank system used to test browser automation.

Flow: /login -> /search -> /member/<id> -> /new-subaccount/<id> -> /confirmation
All data lives in memory (MEMBERS dict below); nothing is persisted to disk.
"""

import random
from functools import wraps

from flask import Flask, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = "mockbank-local-testing-only-not-a-real-secret"

MEMBERS = {
    "10001": {"name": "Alice Johnson", "balance": 4200.50},
    "10002": {"name": "Bob Smith", "balance": 980.00},
    "10003": {"name": "Carol Diaz", "balance": 15340.75},
}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


@app.route("/")
def index():
    if session.get("logged_in"):
        return redirect(url_for("search"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        session["logged_in"] = True
        session["username"] = request.form.get("username", "")
        return redirect(url_for("search"))
    return render_template("login.html")


@app.route("/search", methods=["GET", "POST"])
@login_required
def search():
    error = None
    member_id = ""
    if request.method == "POST":
        member_id = request.form.get("member_id", "").strip()
        if member_id in MEMBERS:
            return redirect(url_for("member_detail", member_id=member_id))
        error = "Member not found"
    return render_template("search.html", error=error, member_id=member_id)


@app.route("/member/<member_id>")
@login_required
def member_detail(member_id):
    member = MEMBERS.get(member_id)
    if member is None:
        return redirect(url_for("search"))
    return render_template("member.html", member=member, member_id=member_id)


@app.route("/new-subaccount/<member_id>", methods=["GET", "POST"])
@login_required
def new_subaccount(member_id):
    member = MEMBERS.get(member_id)
    if member is None:
        return redirect(url_for("search"))

    error = None
    deposit_raw = ""
    account_type = "Checking"

    if request.method == "POST":
        account_type = request.form.get("account_type", "Checking")
        deposit_raw = request.form.get("deposit", "")
        try:
            deposit = float(deposit_raw)
        except ValueError:
            deposit = None

        if deposit is None or deposit <= 0:
            error = "Initial deposit must be a positive amount."
        else:
            reference = "REF-%06d" % random.randint(0, 999999)
            session["last_confirmation"] = {
                "reference": reference,
                "member_id": member_id,
                "member_name": member["name"],
                "account_type": account_type,
                "deposit": deposit,
            }
            return redirect(url_for("confirmation"))

    return render_template(
        "new_subaccount.html",
        member=member,
        member_id=member_id,
        error=error,
        deposit=deposit_raw,
        account_type=account_type,
    )


@app.route("/confirmation")
@login_required
def confirmation():
    data = session.get("last_confirmation")
    if not data:
        return redirect(url_for("search"))
    return render_template("confirmation.html", **data)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
