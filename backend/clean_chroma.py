import chromadb

client = chromadb.PersistentClient(path='./chroma_store')
col = client.get_collection('sop_docs')

results = col.get()
total = len(results['ids'])
print(f"Total chunks in DB: {total}")

# Find chunks that contain bad data from the bad escalation write
bad_ids = [
    results['ids'][i]
    for i, doc in enumerate(results['documents'])
    if '500 pieces' in doc or 'How do I apply for leave' in doc
]

if bad_ids:
    col.delete(ids=bad_ids)
    print(f"Deleted {len(bad_ids)} bad chunk(s):")
    for bid in bad_ids:
        print(f"  {bid}")
else:
    print("No bad chunks found.")