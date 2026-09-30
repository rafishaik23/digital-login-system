from flask import Flask, render_template, request, redirect, session, flash
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import os
import secrets

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "development-only-change-me")
LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))


def to_ist(dt):
    """Return a timezone-aware datetime normalized to Indian Standard Time."""
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def format_ist_time(value):
    """Format a datetime/string as an IST clock time like '4:38 PM'."""
    dt = to_ist(value)
    if dt is None:
        return "Unavailable"
    return dt.strftime("%I:%M %p").lstrip("0")


# ---------------- MONGODB DATABASE ----------------
# Required environment variable:
# MONGO_URI = your MongoDB Atlas connection string
# Optional:
# MONGO_DB_NAME = digital_logbook

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "digital_logbook")

if not MONGO_URI:
    raise RuntimeError(
        "MONGO_URI is not set. Add your MongoDB Atlas connection string "
        "to the environment variables."
    )

# Reuse the MongoDB client between warm serverless invocations.
client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
    socketTimeoutMS=10000,
)

db = client[MONGO_DB_NAME]
users_collection = db["users"]
login_logs_collection = db["login_logs"]


def create_indexes():
    """Create the indexes needed by the application."""
    email_index = users_collection.index_information().get("email_1")
    if (
        email_index
        and email_index.get("unique")
        and not email_index.get("partialFilterExpression")
    ):
        users_collection.drop_index("email_1")

    users_collection.create_index(
        "email",
        unique=True,
        partialFilterExpression={"email": {"$type": "string"}},
    )
    users_collection.create_index("checkin_id", unique=True)
    users_collection.create_index(
        "registration_request_id",
        unique=True,
        sparse=True,
    )
    login_logs_collection.create_index(
        [("user_id", 1), ("created_at", -1)]
    )
    login_logs_collection.create_index(
        "visit_token",
        unique=True,
        sparse=True,
    )


def make_checkin_id():
    """Generate the next unique check-in ID using an atomic MongoDB counter."""
    while True:
        with client.start_session() as session:
            try:
                with session.start_transaction():
                    counter = db["counters"].find_one_and_update(
                        {"_id": "checkin_id"},
                        {"$inc": {"value": 1}},
                        upsert=True,
                        return_document=ReturnDocument.AFTER,
                        session=session,
                    ) or {"value": 0}
                    checkin_id = f"skc{counter['value']}"
                    if users_collection.find_one({"checkin_id": checkin_id}, session=session):
                        raise DuplicateKeyError("checkin_id collision")
                    return checkin_id
            except DuplicateKeyError:
                continue
            except Exception:
                continue


def normalize_user(user):
    """Expose MongoDB _id as the template-friendly string field 'id'."""
    if user is None:
        return None
    user["id"] = str(user["_id"])
    return user


def normalize_log(log):
    """Expose MongoDB _id as the template-friendly string field 'id'."""
    if log is None:
        return None
    log["id"] = str(log["_id"])
    return log


def load_visit_context(visit_token):
    """Load the visit and visitor identified by an opaque visit token."""
    if not visit_token:
        return None, None

    log = login_logs_collection.find_one({"visit_token": visit_token})
    if log is None:
        return None, None

    try:
        user = users_collection.find_one({"_id": ObjectId(log["user_id"])})
    except Exception:
        user = None

    return normalize_log(log), normalize_user(user)


def ensure_visit_token(log, checkin_id):
    """Assign a token to an older active visit when it is first reopened."""
    if log is None:
        return None

    visit_token = log.get("visit_token")
    if visit_token:
        return visit_token

    checkin_time = log.get("checkin_time")
    if not checkin_time:
        try:
            checkin_time = datetime.strptime(
                log["login_time"], "%Y-%m-%d %H:%M:%S"
            ).strftime("%I:%M %p").lstrip("0")
        except (KeyError, TypeError, ValueError):
            checkin_time = "Unavailable"

    result = login_logs_collection.find_one_and_update(
        {
            "_id": log["_id"],
            "$or": [
                {"visit_token": {"$exists": False}},
                {"visit_token": None},
            ],
        },
        {
            "$set": {
                "visit_token": secrets.token_urlsafe(32),
                "checkin_id": checkin_id or log.get("checkin_id", ""),
                "checkin_time": checkin_time,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if result:
        return result["visit_token"]

    updated_log = login_logs_collection.find_one({"_id": log["_id"]})
    return updated_log.get("visit_token") if updated_log else None


# MongoDB Atlas is the persistent database; no SQLite file is used.
create_indexes()


# ---------------- HOME ----------------

@app.route("/")
def home():
    for key in ("user_id", "user_name", "checkin_id", "visit_token", "checkin_success"):
        session.pop(key, None)
    return render_template("index.html")


# ---------------- ADMIN ----------------

@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        admin_username = os.environ.get("ADMIN_USERNAME")
        admin_password = os.environ.get("ADMIN_PASSWORD")

        if not admin_username or not admin_password:
            flash("Admin credentials are not configured. Set ADMIN_USERNAME and ADMIN_PASSWORD.")
        elif username == admin_username and password == admin_password:
            session["admin_logged_in"] = True
            return redirect("/admin-dashboard")
        else:
            flash("Invalid Admin Username or Password")

    return render_template("admin-login.html")


@app.route("/admin-dashboard")
def admin_dashboard():
    if not session.get("admin_logged_in"):
        return redirect("/admin-login")

    return render_template("admin-dashboard.html")


@app.route("/admin/users")
def admin_users():
    if not session.get("admin_logged_in"):
        return redirect("/admin-login")

    users = list(users_collection.find({}).sort("created_at", -1))
    users = [normalize_user(user) for user in users]

    return render_template("admin-users.html", users=users)


@app.route("/admin/logbook")
def admin_logbook():
    if not session.get("admin_logged_in"):
        return redirect("/admin-login")

    logs = list(login_logs_collection.find({}).sort("created_at", -1))
    for log in logs:
        try:
            user = users_collection.find_one({"_id": ObjectId(log["user_id"])})
        except (KeyError, TypeError, ValueError):
            user = None

        log["name"] = user.get("name", "Unknown visitor") if user else "Unknown visitor"
        log["checkin_id"] = log.get(
            "checkin_id",
            user.get("checkin_id", "N/A") if user else "N/A",
        )
        normalize_log(log)

    return render_template("admin-logbook.html", logs=logs)


@app.route("/admin-logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    flash("You have checked out. Enter your Check-in ID to start another visit.")
    return redirect("/admin-login")


# ---------------- REGISTER ----------------

@app.route("/register", methods=["GET", "POST"])
def register():
    registration_request_id = secrets.token_urlsafe(32)

    if request.method == "POST":
        registration_request_id = (
            request.form.get("registration_request_id", "").strip()
            or registration_request_id
        )
        existing_user = users_collection.find_one(
            {"registration_request_id": registration_request_id}
        )
        if existing_user:
            session["registration_success"] = {
                "checkin_id": existing_user["checkin_id"]
            }
            return redirect("/register")

        name = request.form.get("name", "").strip()
        age = request.form.get("age", "").strip()
        gender = request.form.get("gender", "").strip()
        contact = request.form.get("contact", "").strip()
        address = request.form.get("address", "").strip()
        qualification = request.form.get("qualification", "").strip()
        email = request.form.get("email", "").strip().lower()

        while True:
            try:
                with client.start_session() as mongo_session:
                    with mongo_session.start_transaction():
                        counter = db["counters"].find_one_and_update(
                            {"_id": "checkin_id"},
                            {"$inc": {"value": 1}},
                            upsert=True,
                            return_document=ReturnDocument.AFTER,
                            session=mongo_session,
                        ) or {"value": 0}
                        checkin_id = f"skc{counter['value']}"
                        user_document = {
                            "checkin_id": checkin_id,
                            "name": name,
                            "age": age,
                            "gender": gender,
                            "contact": contact,
                            "address": address,
                            "qualification": qualification,
                            "registration_request_id": registration_request_id,
                            "created_at": datetime.now(LOCAL_TZ),
                        }
                        if email:
                            user_document["email"] = email
                        users_collection.insert_one(user_document, session=mongo_session)

                session["registration_success"] = {"checkin_id": checkin_id}
                return redirect("/register")
            except DuplicateKeyError as exc:
                existing_user = users_collection.find_one(
                    {"registration_request_id": registration_request_id}
                )
                if existing_user:
                    session["registration_success"] = {
                        "checkin_id": existing_user["checkin_id"]
                    }
                    return redirect("/register")
                if email and "email" in str(exc).lower():
                    return render_template(
                        "register.html",
                        error="An account with this email already exists.",
                        registration_request_id=registration_request_id,
                    )
                continue
            except Exception:
                return render_template(
                    "register.html",
                    error="Registration failed. Please try again.",
                    registration_request_id=registration_request_id,
                )

    registration_success = session.pop("registration_success", None)
    return render_template(
        "register.html",
        registration_success=registration_success,
        registration_request_id=registration_request_id,
    )


# ---------------- LOGIN ----------------

@app.route("/visitor-checkout", methods=["GET", "POST"])
def visitor_checkout():
    checkin_id = request.form.get("checkin_id", "").strip().lower()

    if request.method == "POST":
        user = normalize_user(users_collection.find_one({"checkin_id": checkin_id}))
        if user:
            active_log = login_logs_collection.find_one(
                {
                    "user_id": user["id"],
                    "logout_time": None,
                },
                sort=[("created_at", -1)],
            )
            visit_token = ensure_visit_token(active_log, user["checkin_id"])
            if visit_token:
                return redirect(f"/dashboard?visit_token={visit_token}")

            flash("There is no active visit for this Check-in ID.")
        else:
            flash("Check-in ID not found. Please check it and try again.")

    return render_template("visitor-checkout.html", checkin_id=checkin_id)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        session.pop("checkin_success", None)
        checkin_id = request.form.get("checkin_id", "").strip().lower()
        purpose = request.form.get("purpose", "").strip()

        user = users_collection.find_one({"checkin_id": checkin_id})
        user = normalize_user(user)

        if user:
            active_log = login_logs_collection.find_one(
                {
                    "user_id": user["id"],
                    "logout_time": None,
                },
                sort=[("created_at", -1)],
            )
            if active_log:
                visit_token = ensure_visit_token(
                    active_log,
                    user["checkin_id"],
                )
                if visit_token:
                    flash("You already have an active visit. Continue it or check out before starting another.")
                    return redirect(f"/dashboard?visit_token={visit_token}")

            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["checkin_id"] = user["checkin_id"]

            checked_in_at = datetime.now(LOCAL_TZ)
            login_time = checked_in_at.strftime("%Y-%m-%d %H:%M:%S")
            checkin_time = format_ist_time(checked_in_at)
            visit_token = secrets.token_urlsafe(32)

            login_logs_collection.insert_one({
                "user_id": user["id"],
                "checkin_id": user["checkin_id"],
                "visit_token": visit_token,
                "checkin_time": checkin_time,
                "login_time": login_time,
                "logout_time": None,
                "purpose": purpose,
                "work_done": None,
                "created_at": checked_in_at,
            })

            session["visit_token"] = visit_token
            session["checkin_success"] = {
                "checkin_id": user["checkin_id"],
                "checkin_time": checkin_time,
                "user_name": user["name"],
                "visit_token": visit_token,
            }
            return redirect(f"/check-in-success?visit_token={visit_token}")

        flash("Invalid Check-in ID.")

    return render_template(
        "login.html",
        checkin_id=request.values.get("checkin_id", ""),
    )


@app.route("/check-in-success")
def checkin_success():
    visit_token = request.args.get("visit_token") or session.get("visit_token")
    if visit_token:
        log, user = load_visit_context(visit_token)
        if log is None or user is None:
            return redirect("/login")
        if log.get("logout_time"):
            return redirect(f"/checkout-success?visit_token={visit_token}")
        checkin = {
            "checkin_id": log.get("checkin_id", user["checkin_id"]),
            "checkin_time": log.get("checkin_time", ""),
            "user_name": user["name"],
        }
        return render_template(
            "checkin-success.html",
            checkin=checkin,
            visit_token=visit_token,
        )

    if "user_id" not in session:
        return redirect("/login")

    checkin = session.get("checkin_success")
    if not checkin:
        return redirect("/dashboard")

    checkin = dict(checkin)
    checkin["user_name"] = session.get(
        "user_name",
        checkin.get("user_name", "Visitor"),
    )

    active_log = login_logs_collection.find_one(
        {"user_id": session["user_id"], "logout_time": None},
        sort=[("created_at", -1)],
    )
    visit_token = ensure_visit_token(
        active_log,
        session.get("checkin_id", checkin.get("checkin_id", "")),
    )
    if visit_token:
        session["visit_token"] = visit_token
        return redirect(f"/check-in-success?visit_token={visit_token}")

    return render_template(
        "checkin-success.html",
        checkin=checkin,
        visit_token=None,
    )


# ---------------- DASHBOARD ----------------

@app.route("/dashboard")
def dashboard():
    visit_token = request.args.get("visit_token") or session.get("visit_token")
    if visit_token:
        log, user = load_visit_context(visit_token)
        if log is None or user is None:
            return redirect("/login")
        if log.get("logout_time"):
            return redirect(f"/checkout-success?visit_token={visit_token}")
        if session.get("visit_token") == visit_token:
            session.pop("checkin_success", None)
        return render_template(
            "dashboard.html",
            user=user,
            visit_token=visit_token,
        )

    if "user_id" not in session:
        return redirect("/login")

    session.pop("checkin_success", None)
    try:
        user = users_collection.find_one({
            "_id": ObjectId(session["user_id"])
        })
    except Exception:
        user = None

    user = normalize_user(user)
    if user is None:
        session.clear()
        flash("User account not found.")
        return redirect("/login")

    active_log = login_logs_collection.find_one(
        {"user_id": user["id"], "logout_time": None},
        sort=[("created_at", -1)],
    )
    visit_token = ensure_visit_token(active_log, user["checkin_id"])
    if visit_token:
        session["visit_token"] = visit_token
        return redirect(f"/dashboard?visit_token={visit_token}")

    return render_template("dashboard.html", user=user, visit_token=None)


# ---------------- LOGBOOK ----------------

@app.route("/logbook")
def logbook():
    visit_token = request.args.get("visit_token") or session.get("visit_token")
    if visit_token:
        log, user = load_visit_context(visit_token)
        if log is None or user is None:
            return redirect("/login")
        user_id = user["id"]
        user_name = user["name"]
    else:
        if "user_id" not in session:
            return redirect("/login")
        user_id = session["user_id"]
        user_name = session.get("user_name", "")
        active_log = login_logs_collection.find_one(
            {"user_id": user_id, "logout_time": None},
            sort=[("created_at", -1)],
        )
        visit_token = ensure_visit_token(
            active_log,
            session.get("checkin_id", ""),
        )
        if visit_token:
            session["visit_token"] = visit_token
            return redirect(f"/logbook?visit_token={visit_token}")

    logs = list(
        login_logs_collection.find({
            "user_id": user_id
        }).sort("created_at", -1)
    )
    logs = [normalize_log(log) for log in logs]

    return render_template(
        "logbook.html",
        logs=logs,
        user_name=user_name,
        visit_token=visit_token,
    )


# ---------------- LOGOUT ----------------

@app.route("/logout", methods=["GET", "POST"])
def logout():
    visit_token = request.values.get("visit_token") or session.get("visit_token")
    if visit_token:
        log = login_logs_collection.find_one({
            "visit_token": visit_token,
            "logout_time": None,
        })
        if log is None:
            existing_log = login_logs_collection.find_one({"visit_token": visit_token})
            if existing_log and existing_log.get("logout_time"):
                return redirect(f"/checkout-success?visit_token={visit_token}")
    else:
        if "user_id" not in session:
            return redirect("/login")
        log = login_logs_collection.find_one(
            {
                "user_id": session["user_id"],
                "logout_time": None,
            },
            sort=[("created_at", -1)]
        )
        visit_token = ensure_visit_token(
            log,
            session.get("checkin_id", ""),
        )
        if visit_token:
            session["visit_token"] = visit_token
    log = normalize_log(log)

    if log is None:
        if not visit_token:
            session.clear()
        flash("No active visit was found. Please check in before checking out.")
        return redirect("/login")

    if request.method == "GET":
        return render_template("logout.html", visit_token=visit_token)

    checked_out_at = datetime.now(LOCAL_TZ)
    logout_time = checked_out_at.strftime("%Y-%m-%d %H:%M:%S")
    work_done = request.form.get("work_done", "").strip()

    login_logs_collection.update_one(
        {"_id": log["_id"]},
        {
            "$set": {
                "logout_time": logout_time,
                "work_done": work_done,
            }
        }
    )

    checkin_time = log.get("checkin_time")
    if not checkin_time:
        try:
            checkin_time = format_ist_time(log["login_time"])
        except (KeyError, TypeError, ValueError):
            checkin_time = "Unavailable"

    checkout = {
        "checkin_id": log.get("checkin_id") or session.get("checkin_id", ""),
        "checkin_time": checkin_time,
        "checkout_time": format_ist_time(checked_out_at),
    }
    if visit_token:
        if session.get("visit_token") == visit_token:
            session.clear()
        return redirect(f"/checkout-success?visit_token={visit_token}")

    session.clear()
    session["checkout_success"] = checkout
    return redirect("/checkout-success")


@app.route("/checkout-success")
def checkout_success():
    visit_token = request.args.get("visit_token")
    if visit_token:
        log = login_logs_collection.find_one({"visit_token": visit_token})
        if log is None or not log.get("logout_time"):
            return redirect("/")

        checkin_time = log.get("checkin_time")
        if not checkin_time:
            try:
                checkin_time = format_ist_time(log["login_time"])
            except (KeyError, TypeError, ValueError):
                checkin_time = "Unavailable"

        try:
            checkout_time = format_ist_time(log["logout_time"])
        except (TypeError, ValueError):
            checkout_time = log["logout_time"]

        checkout = {
            "checkin_id": log.get("checkin_id", ""),
            "checkin_time": checkin_time,
            "checkout_time": checkout_time,
        }
        return render_template("checkout-success.html", checkout=checkout)

    checkout = session.pop("checkout_success", None)
    if not checkout:
        return redirect("/")

    return render_template("checkout-success.html", checkout=checkout)


# ---------------- RUN APPLICATION ----------------
# Local development only. For Vercel, api/index.py imports this app.

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        use_reloader=False
    )
