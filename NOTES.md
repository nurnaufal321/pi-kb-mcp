# Captured API shapes

Observed live on 2026-09-05 from an authenticated portal session by intercepting
the SPA's own `fetch` calls. No credentials are recorded here.

## Hosts

- Portal (SPA + SharePoint/ADFS): `https://softwaresupportsp.aveva.com`
- API: `https://services.softwaresupport.aveva.com/api`
- Search base (`B`): `https://services.softwaresupport.aveva.com/api/search/api/v1`

## Auth

Every API call carries a JWT bearer:

```
Authorization: Bearer <JWT>
Accept: application/json, text/plain, */*
Content-Type: application/json          # POST only
```

JWT claims: `sub`/`samaccountname`, `email`, `techsupportid`, `iss=http://jwtauthsrv.gcsdev.com`.
**`exp - nbf` = 28800s = exactly 8 hours.**

Login chain: ADFS (`extlogon.aveva.com`, WS-Fed) -> `/_trust` -> FedAuth cookie
(HttpOnly, not visible to JS) -> SPA holds the JWT **in memory only** (nothing in
localStorage/sessionStorage/document.cookie).

### Open question: how the JWT is minted

`/_layouts/15/BerlinTokenIssuer/TokenIssuer.asmx/GetToken` appears in the page's
resource timings at boot, but **every direct call returns HTTP 500 with an empty
body** — authenticated or not, and for all of: JSON `{}`, JSON no body, form-encoded,
and GET. A fresh JWT *is* minted on each full page load (two boots 69s apart produced
different `nbf`), so a working path exists; it is not reproducible by calling that
endpoint directly.

Consequence: capture the bearer by **observing the app's own boot request** rather than
replicating the mint. See README for which deployment modes this supports.

## Search

`POST {B}/Search`

```json
{
  "filters": [
    {"fieldName": "cs_languages", "groupName": "Languages",
     "values": [{"name": "English", "isSelected": true}]}
  ],
  "dateRangeFilters": [],
  "q": "PI Vision authentication",
  "pgsz": 50,
  "pg": 1,
  "debug": false,
  "showFavorites": false,
  "hideEmployeeOnlyContent": true,
  "sortField": "relevancy",
  "sortOrder": "desc"
}
```

Response: `{httpStatusCode, message, result}` where `result` has
`totalItems, pageIndex, pageSize, sortField, sortOrder, searchFacets, items, metadata, debug`.

`items[]`:

```
uniqueId, url, title, excerpt, contentSource, contentType, language,
hasQuickView, relevance, percentageRelevance, visibility[], products[],
publishedDate, isPadLocked, metadata{ArticleNo, ArticleLanguage, Confidence}
```

`searchFacets[]` groups (`fieldName`/`groupName`/`values[].name`):

- **Content Source** — `Knowledge Base`, `Online Product Documentation`, `Community`, `Product News`
  -> filter on `Knowledge Base` to scope to KB articles.
- **Languages**
- **Products** — full product list incl. `PI Vision`, used for the `product` argument.

`POST {B}/Search/QuerySuggestions` -> `{"q": "...", "numberOfResults": 5}` (typeahead; not needed).

## Article detail

`GET {B}/KnowledgeBase/KBArticle?docID=000100024&lang=en_US`

Returns the article object **unwrapped** (no `result` envelope — differs from Search):

```
articleId, articleNumber, legacyDocId, articleType, confidence, title,
body (HTML, ~5KB), articleVisibility, language, isMasterLanguage,
availableTranslations[], versionNumber, firstPublished, lastUpdatedOn,
products[], fileAttachment, message, foundInVersion, resolvedInVersion,
knownIssueStatus, osiArticleNumber
```

`body` is HTML -> run through `html_to_text()`.

Public article URL: `https://softwaresupportsp.aveva.com/en-US/knowledge/details/<articleNumber>?lang=en_US`
