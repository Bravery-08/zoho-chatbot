import sqlite3

con = sqlite3.connect('data/escalations.db')

rows = con.execute(
    "SELECT id, customer_jid, question FROM escalations WHERE status='pending'"
).fetchall()

print(f"Pending escalations: {len(rows)}")
for r in rows:
    print(f"  id={r[0][:8]}... | customer={r[1]} | question={r[2][:60]}")

if rows:
    con.execute("UPDATE escalations SET status='expired' WHERE status='pending'")
    con.commit()
    print("All cleared.")
else:
    print("Nothing to clear.")

con.close()