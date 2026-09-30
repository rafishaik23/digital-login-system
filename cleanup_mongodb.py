import os

from dotenv import load_dotenv
from pymongo import MongoClient


def main():
    load_dotenv()
    mongo_uri = os.environ.get("MONGO_URI")
    database_name = os.environ.get("MONGO_DB_NAME", "digital_logbook")

    if not mongo_uri:
        raise RuntimeError("MONGO_URI is not set in the environment or .env file.")

    confirmation = f"DELETE ALL VISITOR DATA FROM {database_name}"

    with MongoClient(mongo_uri, serverSelectionTimeoutMS=10000) as client:
        client.admin.command("ping")
        database = client[database_name]
        users = database["users"]
        visits = database["login_logs"]
        counters = database["counters"]

        print(f"Database: {database_name}")
        print(f"Registered visitors: {users.count_documents({})}")
        print(f"Visit records: {visits.count_documents({})}")
        print(f"Check-in counter: {counters.find_one({'_id': 'checkin_id'})}")
        print("This permanently deletes all registered visitors and visit history.")

        if input(f"Type {confirmation!r} to continue: ") != confirmation:
            print("Cleanup cancelled.")
            return

        deleted_visits = visits.delete_many({}).deleted_count
        deleted_users = users.delete_many({}).deleted_count
        reset_counter = counters.delete_one({"_id": "checkin_id"}).deleted_count

        print(f"Deleted visit records: {deleted_visits}")
        print(f"Deleted registered visitors: {deleted_users}")
        print(f"Removed check-in counter: {bool(reset_counter)}")
        print("The next generated check-in ID will be skc1.")


if __name__ == "__main__":
    main()