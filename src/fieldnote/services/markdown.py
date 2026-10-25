"""Small escaped Markdown subset for the portable reading document.

No authored HTML, images, scripts, styles or embeds are interpreted. Code is
literal. This is a reading/copy aid, not a promise of Medium import fidelity.
"""

import html
import re

from fieldnote.services.urls import validate_public_url

INLINE = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*|(?<!\w)\*[^*\n]+\*|\[[^\[\]\n]{1,240}\]\(<?https?://[^)\s]{1,2048}>?\))")


def table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def table_header(line: str, separator: str) -> bool:
    cells = table_cells(line)
    separators = table_cells(separator)
    return (line.strip().startswith("|") and 2 <= len(cells) <= 12
            and len(cells) == len(separators)
            and all(re.fullmatch(r":?-{3,}:?", cell) for cell in separators))


def inline_html(text: str) -> str:
    parts = []
    for part in INLINE.split(text):
        if part.startswith("`") and part.endswith("`"):
            parts.append("<code>" + html.escape(part[1:-1]) + "</code>")
        elif part.startswith("**") and part.endswith("**"):
            parts.append("<strong>" + html.escape(part[2:-2]) + "</strong>")
        elif part.startswith("*") and part.endswith("*"):
            parts.append("<em>" + html.escape(part[1:-1]) + "</em>")
        elif match := re.fullmatch(r"\[([^\]]+)\]\(<?(https?://[^)>]+)>?\)", part):
            try:
                url = validate_public_url(match[2])
            except ValueError:
                parts.append(html.escape(part))
            else:
                parts.append('<a rel="noopener noreferrer" href="' + html.escape(url, quote=True) + '">' + html.escape(match[1]) + '</a>')
        else:
            parts.append(html.escape(part))
    return "".join(parts)


def markdown_html(markdown: str) -> str:
    parts: list[str] = []
    lines = markdown.splitlines()
    index = 0
    paragraph: list[str] = []
    list_tag = ""

    def flush() -> None:
        nonlocal list_tag
        if paragraph:
            parts.append("<p>" + inline_html(" ".join(paragraph)) + "</p>")
            paragraph.clear()
        if list_tag:
            parts.append(f"</{list_tag}>")
            list_tag = ""

    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith("```"):
            flush()
            code = []
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            parts.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
        elif index < len(lines) and table_header(line, lines[index]):
            flush()
            cells = table_cells(line)
            parts.append("<table><thead><tr>" + "".join('<th scope="col">' + inline_html(c) + "</th>" for c in cells) + "</tr></thead><tbody>")
            index += 1
            while index < len(lines) and lines[index].strip().startswith("|") and len(table_cells(lines[index])) == len(cells):
                parts.append("<tr>" + "".join("<td>" + inline_html(c) + "</td>" for c in table_cells(lines[index])) + "</tr>")
                index += 1
            parts.append("</tbody></table>")
        elif heading := re.match(r"^(#{1,6})\s+(.+)$", line):
            flush()
            level = len(heading[1])
            parts.append(f"<h{level}>" + inline_html(heading[2]) + f"</h{level}>")
        elif item := re.match(r"^\s*(?:([-*])|\d+[.)])\s+(.+)$", line):
            tag = "ul" if item[1] else "ol"
            if paragraph or (list_tag and list_tag != tag):
                flush()
            if not list_tag:
                list_tag = tag
                parts.append(f"<{tag}>")
            parts.append("<li>" + inline_html(item[2]) + "</li>")
        elif quote := re.match(r"^>\s?(.*)$", line):
            flush()
            parts.append("<blockquote><p>" + inline_html(quote[1]) + "</p></blockquote>")
        elif re.fullmatch(r"\s*---+\s*", line):
            flush()
            parts.append("<hr>")
        elif not line.strip():
            flush()
        else:
            if list_tag:
                flush()
            paragraph.append(line)
    flush()
    return "\n".join(parts)
