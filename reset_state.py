from pathlib import Path
import json

STATE_FILE = Path("sent_news.json")

def main():
    STATE_FILE.write_text(
        json.dumps({"sent": []}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("=" * 60)
    print("RESET COMPLETE")
    print("All previously recorded/sent news URLs were cleared.")
    print(f"State file: {STATE_FILE.resolve()}")
    print("=" * 60)

if __name__ == "__main__":
    main()
