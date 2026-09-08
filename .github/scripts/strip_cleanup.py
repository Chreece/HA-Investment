from pathlib import Path

path = Path("custom_components/investment/storage.py")
text = path.read_text(encoding="utf-8")
start = text.find("        # Remove data written by the optional feature branch from the release line.\n")
end = text.find('        raw_exposed = user.get("exposed_entities")\n', start)
if start >= 0 and end > start:
    text = text[:start] + text[end:]
path.write_text(text, encoding="utf-8")
