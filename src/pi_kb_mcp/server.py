"""AVEVA Customer Support Portal knowledge base MCP server.

Proxies the portal's search API so LLMs can ground answers in AVEVA's KB
articles — tech notes, known issues, workarounds — which are not part of the
public product documentation at docs.aveva.com.

Requires an AVEVA support entitlement; see `pi-kb-mcp login`.
"""

import re
from typing import Annotated, Any

import httpx
from bs4 import BeautifulSoup
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .auth import AuthError, auth_headers_async

API_ROOT = "https://services.softwaresupport.aveva.com/api"
SEARCH_BASE = f"{API_ROOT}/search/api/v1"
PRODUCTS_URL = f"{API_ROOT}/products/api/Products/GetAllProductsList"
PORTAL = "https://softwaresupportsp.aveva.com"

# The Content Source facet value that isolates KB articles from product docs,
# community posts and product news. Verified live — see NOTES.md.
KB_SOURCE = "Knowledge Base"

# AVEVA lists 1100+ products; this narrows the default listing to the PI family.
PI_PRODUCT = re.compile(r"\bPI\b|\bOSIsoft\b|\bAsset Framework\b", re.I)

# Cap on names returned in one listing, so the tool cannot flood the context.
MAX_LISTED = 100

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

mcp = MCPServer(
    "pi-kb-mcp",
    version="0.1.0",
    website_url="https://github.com/nurnaufal321/pi-kb-mcp",
    instructions=(
        "Searches AVEVA's Customer Support knowledge base — tech notes, known "
        "issues and workarounds for the PI System and other AVEVA products. "
        "Reach for these tools when something is broken or an error message is "
        "quoted; use product-documentation tools for how a feature is meant to "
        "work. All tools are read-only and need a signed-in AVEVA support "
        "session (`pi-kb-mcp login`)."
    ),
)

_client: httpx.AsyncClient | None = None
_products_cache: list[str] | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    return _client


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n\n", text)
    return text.strip()


def article_number(value: str) -> str | None:
    """Accept a bare article number or any portal URL containing one."""
    value = value.strip()
    if re.fullmatch(r"\d{6,}", value):
        return value
    m = re.search(r"/knowledge/details/(\d{6,})", value)
    return m.group(1) if m else None


def article_url(number: str) -> str:
    return f"{PORTAL}/en-US/knowledge/details/{number}?lang=en_US"


def _filters(products: list[str] | None) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = [
        {
            "fieldName": "cs_languages",
            "groupName": "Languages",
            "values": [{"name": "English", "isSelected": True}],
        },
        {
            "fieldName": "cs_contentsource",
            "groupName": "Content Source",
            "values": [{"name": KB_SOURCE, "isSelected": True}],
        },
    ]
    if products:
        filters.append(
            {
                # Yes, misspelled — that is the field name AVEVA's API uses.
                "fieldName": "cs_synonymns",
                "groupName": "Products",
                "values": [{"name": p, "isSelected": True} for p in products],
            }
        )
    return filters


def _search_body(query: str, n: int, products: list[str] | None) -> dict[str, Any]:
    return {
        "filters": _filters(products),
        "dateRangeFilters": [],
        "q": query,
        "pgsz": n,
        "pg": 1,
        "debug": False,
        "showFavorites": False,
        "hideEmployeeOnlyContent": True,
        "sortField": "relevancy",
        "sortOrder": "desc",
    }


def _explain(exc: Exception) -> str:
    """Turn transport failures into something a model can act on."""
    if isinstance(exc, AuthError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 401:
            return "AVEVA portal session rejected (401). Run `pi-kb-mcp login` and retry."
        return f"AVEVA API error {exc.response.status_code} for {exc.request.url}"
    if isinstance(exc, httpx.RequestError):
        return f"Network error contacting the AVEVA portal: {exc}"
    raise exc


async def _post(path: str, body: dict) -> dict:
    resp = await get_client().post(
        f"{SEARCH_BASE}{path}",
        json=body,
        headers={**(await auth_headers_async()), "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()


@mcp.tool(
    description=(
        "Search AVEVA's Customer Support knowledge base and return matching "
        "article numbers, titles, products and excerpts.\n\n"
        "Use this for support-desk material: specific error messages, known issues, "
        "version incompatibilities, workarounds and how-to notes. Prefer product "
        "documentation tools for conceptual or reference questions (what a feature "
        "is, configuration reference, API syntax); reach here when the user reports "
        "something broken or quotes an error.\n\n"
        "Read-only; no side effects. Scoped to English-language Knowledge Base "
        "articles, excluding product documentation, community posts and product "
        "news. Requires a signed-in AVEVA support session — with none, it returns "
        "an instruction to run `pi-kb-mcp login` rather than failing silently. "
        "Returns a match count followed by up to n_results entries, each carrying "
        "the article number that get_kb_article needs."
    ),
    annotations=READ_ONLY,
)
async def search_pi_kb(
    query: Annotated[str, Field(description=(
        "Free-text search terms. Quoting a verbatim error message works well, "
        "e.g. 'Index was out of range' or 'PI Vision authentication Kerberos'."
    ))],
    n_results: Annotated[int, Field(
        default=5, ge=1, le=50,
        description="Number of articles to return, 1-50.",
    )] = 5,
    products: Annotated[list[str] | None, Field(
        default=None,
        description=(
            "Optional product names to narrow the search, matched exactly against "
            "AVEVA's product list, e.g. ['PI Vision'] or ['PI Data Archive', "
            "'PI Asset Framework']. Call list_kb_products for valid values. Omit "
            "to search all products."
        ),
    )] = None,
) -> str:
    try:
        data = await _post("/Search", _search_body(query, n_results, products))
    except Exception as exc:
        return _explain(exc)

    result = data.get("result") or {}
    items = result.get("items") or []
    if not items:
        return (
            f"No knowledge base results for: {query}\n"
            "Try fewer or more general terms, or drop the products filter."
        )

    lines = [f"{result.get('totalItems', len(items))} matches; showing {len(items)}\n"]
    for i, item in enumerate(items, 1):
        number = (item.get("metadata") or {}).get("ArticleNo") or ""
        prods = ", ".join(item.get("products") or []) or "—"
        updated = (item.get("publishedDate") or "")[:10]
        excerpt = html_to_text(item.get("excerpt") or "")[:250]

        lines.append(f"{i}. {item.get('title', 'Untitled')} [{number}]")
        lines.append(f"   {prods} · updated {updated}")
        lines.append(f"   {article_url(number) if number else item.get('url', '')}")
        if excerpt:
            lines.append(f"   {excerpt}")

    return "\n".join(lines)


@mcp.tool(
    description=(
        "Fetch the full text of one AVEVA knowledge base article, including its "
        "symptoms, cause and resolution sections.\n\n"
        "Use after search_pi_kb to read an article whose excerpt looks relevant, or "
        "directly whenever the user supplies an article number or a "
        "softwaresupportsp.aveva.com link. Search excerpts are truncated and "
        "routinely omit the actual fix, so read the article before answering from a "
        "search result alone.\n\n"
        "Read-only; no side effects. Returns a header line with title, products, "
        "article type and last-updated date, then the article body converted from "
        "HTML to plain text and truncated at max_chars, with a marker when cut."
    ),
    annotations=READ_ONLY,
)
async def get_kb_article(
    article: Annotated[str, Field(description=(
        "Article number such as '000100024', or a full portal URL such as "
        "'https://softwaresupportsp.aveva.com/en-US/knowledge/details/000100024'. "
        "Both forms are accepted."
    ))],
    max_chars: Annotated[int, Field(
        default=4000, ge=500, le=20000,
        description=(
            "Maximum characters of article body to return, 500-20000. Raise it if "
            "the response ends in a truncation marker."
        ),
    )] = 4000,
) -> str:
    number = article_number(article)
    if not number:
        return (
            f"Could not read an article number from: {article}\n"
            "Expected '000100024' or a .../knowledge/details/000100024 URL."
        )

    try:
        resp = await get_client().get(
            f"{SEARCH_BASE}/KnowledgeBase/KBArticle",
            params={"docID": number, "lang": "en_US"},
            headers=await auth_headers_async(),
        )
        resp.raise_for_status()
    except Exception as exc:
        return _explain(exc)

    data = resp.json()
    body = data.get("body")
    if not body:
        reason = data.get("message") or "no content returned"
        return (
            f"Article {number} unavailable ({reason}). It may not exist, or your "
            "support entitlement may not cover it."
        )

    prods = ", ".join(data.get("products") or []) or "—"
    content = html_to_text(body)
    truncated = len(content) > max_chars

    header = (
        f"# {data.get('title', '')} [{number}]\n"
        f"{prods} · {data.get('articleType') or ''} · "
        f"updated {(data.get('lastUpdatedOn') or '')[:10]}\n"
        f"{article_url(number)}\n\n"
    )
    suffix = "\n\n[truncated — raise max_chars for more]" if truncated else ""
    return header + content[:max_chars] + suffix


@mcp.tool(
    description=(
        "List AVEVA product names accepted by the products argument of "
        "search_pi_kb, optionally filtered by a substring.\n\n"
        "Use when a search returns results spanning unrelated products and you need "
        "the exact spelling to narrow it, or when the user names a product "
        "informally ('PI AF', 'Data Archive') and you need AVEVA's canonical form. "
        "Not needed for a first, unscoped search.\n\n"
        "Read-only; no side effects. AVEVA publishes over a thousand product names, "
        "so with no argument this returns only PI System products; pass `contains` "
        "to find products outside that set. Results are cached for the life of the "
        "process. Returns one name per line, alphabetically, or a note if nothing "
        "matched."
    ),
    annotations=READ_ONLY,
)
async def list_kb_products(
    contains: Annotated[str | None, Field(
        default=None,
        description=(
            "Case-insensitive substring to filter product names, e.g. 'vision', "
            "'historian' or 'adapter'. Omit to list PI System products only."
        ),
    )] = None,
) -> str:
    global _products_cache
    if _products_cache is None:
        try:
            resp = await get_client().get(PRODUCTS_URL, headers=await auth_headers_async())
            resp.raise_for_status()
        except Exception as exc:
            return _explain(exc)
        data = resp.json()
        names = sorted({
            (item.get("name") or "").strip()
            for item in (data if isinstance(data, list) else [])
            if (item.get("name") or "").strip()
        })
        if not names:
            return "Product list unavailable; search without the products filter."
        _products_cache = names

    names = _products_cache
    if contains:
        needle = contains.strip().lower()
        matched = [n for n in names if needle in n.lower()]
        label = f"products matching {contains!r}"
    else:
        matched = [n for n in names if PI_PRODUCT.search(n)]
        label = "PI System products"

    if not matched:
        return (
            f"No products matching {contains!r}. "
            f"AVEVA lists {len(names)} products in total; try a shorter substring."
        )

    shown = matched[:MAX_LISTED]
    header = (
        f"{len(matched)} {label} (of {len(names)} total) — pass any of these "
        "verbatim to search_pi_kb(products=[...])"
    )
    if len(matched) > len(shown):
        header += (
            f"\nShowing the first {len(shown)}; narrow with `contains` "
            "(e.g. contains='vision') to see the rest."
        )
    elif not contains:
        header += "\nPass `contains` to search the full product list."
    return header + "\n\n" + "\n".join(shown)


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
