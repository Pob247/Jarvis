import chromadb

print("--- INITIALIZING MEMORY CORE ---")

# 1. Setup the database on your disk
# This will create a real folder called 'jarvis_memory' on your Desktop.
client = chromadb.PersistentClient(path="jarvis_memory")

# 2. Create a collection (Think of this as a "File Cabinet")
collection = client.get_or_create_collection(name="user_preferences")

# 3. MEMORIZE: We will teach it two distinct facts.
print(">> Memorizing facts...")
collection.upsert(
    documents=[
        "The user loves technical automation and coding in Python.",
        "The user hates marketing spam and newsletters about sales."
    ],
    ids=["preference_1", "preference_2"]
)

# 4. RECALL: We ask a question that DOES NOT use the exact words.
# Notice: We ask about "writing scripts", not "automation" or "Python".
query = "How does the user feel about writing scripts?"
print(f"\n>> User asks: '{query}'")

results = collection.query(
    query_texts=[query],
    n_results=1
)

best_memory = results['documents'][0][0]
print(f">> Jarvis Recalls: {best_memory}")