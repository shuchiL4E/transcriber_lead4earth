# app/db.py
import os
from pymongo import MongoClient
from dotenv import load_dotenv
import os

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)

# Database and collection
db = client["transcriber_db"]
transcripts_collection = db["transcripts"]
