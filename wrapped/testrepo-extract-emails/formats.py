import re
def slugify(text: str) -> str:
    """Turn a title into a url slug."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

def extract_emails(text: str) -> list:
    """Find every email address in a block of text."""
    return re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
