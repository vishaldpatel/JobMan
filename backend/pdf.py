import markdown as markdown_lib
from weasyprint import HTML

CSS = """
@page {
    size: Letter;
    margin: 0.75in;
}
body {
    font-family: "Liberation Sans", "Helvetica Neue", Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.45;
    color: #1a1a1a;
}
h1 {
    font-size: 19pt;
    margin: 0 0 0.15em;
    color: #111;
}
h2 {
    font-size: 12.5pt;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #333;
    border-bottom: 1px solid #ccc;
    padding-bottom: 0.15em;
    margin: 1.1em 0 0.4em;
}
h3 {
    font-size: 11pt;
    margin: 0.8em 0 0.2em;
}
p {
    margin: 0.4em 0;
}
ul, ol {
    margin: 0.3em 0 0.6em;
    padding-left: 1.3em;
}
li {
    margin: 0.15em 0;
}
strong {
    color: #000;
}
a {
    color: #1a5fb4;
    text-decoration: none;
}
hr {
    border: none;
    border-top: 1px solid #ccc;
    margin: 0.8em 0;
}
"""


def markdown_to_pdf(markdown_text: str) -> bytes:
    html_body = markdown_lib.markdown(markdown_text, extensions=["extra", "sane_lists"])
    html = f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{html_body}</body></html>"
    return HTML(string=html).write_pdf()
