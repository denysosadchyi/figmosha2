# Figma Comments — Read & Act Pipeline

Read Figma comments via REST API and apply requested changes via Scripter.

## Prerequisites

**Figma Personal Access Token** is required. Ask the user:

> To read Figma comments, I need a Figma REST API token. You can generate one at https://www.figma.com/developers/api#access-tokens — paste it here.

Store the token in memory for the session. Do NOT hardcode it in scripts or commit to git.

## Pipeline

```
Fetch unresolved comments → Parse node & message → Apply changes via Scripter → Verify
```

## Step 1 — Fetch unresolved comments

```bash
curl -s -H "X-Figma-Token: TOKEN" \
  "https://api.figma.com/v1/files/FILE_KEY/comments" | \
  python3 -c "
import json, sys
data = json.load(sys.stdin)
for c in data['comments']:
    if not c.get('resolved_at'):
        print('ID:', c['id'])
        print('User:', c['user']['handle'])
        print('Message:', c['message'])
        print('Node:', c.get('client_meta',{}).get('node_id','?'))
        print('---')
"
```

Extract `FILE_KEY` from the Figma URL: `figma.com/design/:fileKey/:fileName`

## Step 2 — Understand the comment

Comments are attached to a node (`client_meta.node_id`). Common patterns:

| Comment pattern | Action |
|---|---|
| "заміни текст на X" / "change text to X" | Update `characters` on the target text node |
| "колір на X" / "change color" | Update fills on the target node |
| "видали" / "remove" | Remove the target node |
| "додай X" / "add X" | Create new node near the commented area |

The `node_id` in the comment points to the frame/group the comment is pinned to, NOT necessarily the exact text node. You may need to search children of that node.

## Step 3 — Apply changes via Scripter

For text changes (most common):

```js
// Find the right text node inside the commented frame
const parent = figma.getNodeById("COMMENT_NODE_ID");
const texts = parent.findAll(n => n.type === "TEXT");
// Find by current content or name
const target = texts.find(t => t.characters === "Old text");

// Load font from node itself (critical for custom fonts like Gilroy!)
figma.loadFontAsync(target.fontName).then(() => {
  target.characters = "New text";
  figma.notify("OK: " + target.characters);
}).catch(e => figma.notify("ERR: " + e.message, {error:true}));
```

**Important:** Always read the font from the node via `n.fontName` — never hardcode font names. Custom/team fonts (Gilroy, etc.) fail with manually constructed `{family, style}` objects.

## Step 4 — Verify

```js
const n = figma.getNodeById("NODE_ID");
figma.notify("Verify: " + n.characters);
```

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/v1/files/:key/comments` | GET | List all comments |
| `/v1/files/:key/comments` | POST | Add a comment |
| `/v1/files/:key/comments/:id` | DELETE | Delete a comment |
| `/v1/files/:key` | GET | File metadata |
| `/v1/files/:key/nodes?ids=:id` | GET | Specific node data |

All requests require header: `X-Figma-Token: <personal-access-token>`

## Tips

- Filter unresolved comments: check `resolved_at` is null/empty
- Comment `node_id` is the parent frame — search its children for the actual target
- Multiple comments may reference the same node — process all at once
- After applying changes, user can resolve comments manually in Figma
