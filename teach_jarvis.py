import chromadb
import uuid
import time

# Connect to the SAME memory folder as your agent
print("--- CONNECTING TO NEURAL LINK ---")
client = chromadb.PersistentClient(path="jarvis_memory")
collection = client.get_or_create_collection(name="user_preferences")

def list_memories():
    print("\n--- CURRENT MEMORIES ---")
    # Get all memories (limit to 10 so it doesn't flood screen)
    data = collection.get(limit=10)
    if not data['documents']:
        print("(Memory is empty)")
    else:
        for i, doc in enumerate(data['documents']):
            print(f" [{i+1}] {doc}")
    print("------------------------")

def add_memory():
    new_fact = input("\nWhat should I remember? (or type 'exit'): ")
    
    if new_fact.lower() == 'exit':
        return False
    
    # Create a unique ID for this memory
    mem_id = str(uuid.uuid4())
    
    print(">> Encoding memory into vector space...")
    collection.add(
        documents=[new_fact],
        ids=[mem_id]
    )
    print(">> Memory Stored.")
    return True

# --- MAIN LOOP ---
while True:
    print("\nOPTIONS:")
    print("1. Teach me a new fact")
    print("2. View existing memories")
    print("3. Exit")
    
    choice = input("Select (1/2/3): ")
    
    if choice == "1":
        while add_memory():
            pass # Keep asking until user types exit
    elif choice == "2":
        list_memories()
    elif choice == "3":
        print("Exiting Teacher Mode.")
        break
    else:
        print("Invalid choice.")