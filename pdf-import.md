# PDF Import to Figma — Pipeline

Transfer PDF presentation slides into Figma as structured frames via Scripter.

## When to use

User provides a PDF file and a Figma target node (page or frame) and wants the presentation recreated in Figma.

## Pipeline

```
Read PDF → Analyze slides → Create frames (1 script per slide) → Verify
```

## Step 0 — Read PDF

Read the PDF page by page (5 pages at a time) to see all slides visually. Note for each slide:
- Title and all text content (headings, body, labels, footnotes)
- Layout structure (columns, rows, sections, cards)
- Images, charts, graphs → will become placeholder rectangles
- Branding elements (logo, accent colors, recurring patterns)

## Step 1 — Create slides

One Scripter script per slide. Each script:

```js
(async () => {
const page = figma.getNodeById("TARGET_NODE_ID");
await figma.loadFontAsync({family:"Inter",style:"Bold"});
await figma.loadFontAsync({family:"Inter",style:"Regular"});
await figma.loadFontAsync({family:"Inter",style:"Semi Bold"});

const f = figma.createFrame();
page.appendChild(f);
f.name = "Slide 01 — Title Here";
f.resize(1920, 1080);    // or whatever size specified
f.x = 0;
f.y = SLIDE_INDEX * (1080 + 100);  // vertical stack, 100px gap
f.fills = [{type:"SOLID",color:{r:1,g:1,b:1}}];

// ... content here ...

figma.notify("Slide N done");
})();
```

### Rules

1. **Frame size**: use the size specified by user (default 1920x1080 for presentations)
2. **Vertical stacking**: each slide at `y = slideIndex * (height + 100)`
3. **Naming**: `"Slide NN — Title"` for easy navigation
4. **Text transfer**: preserve all text content, hierarchy, and approximate positioning
5. **Images → placeholders**: replace with gray rectangles (`fill: 0.92, 0.92, 0.92`), optionally with border and "Image placeholder" name
6. **Charts → placeholders**: simplified representations — gray frames with basic bar/line shapes, or just labeled rectangles
7. **Branding**: keep consistent logo, accent colors, title underlines across all slides
8. **Use `--file`**: always write to `/tmp/slideNN.js` and run via `python run.py --file /tmp/slideNN.js`
9. **Sequential execution**: `sleep 4` between slides to let Figma process

### Text hierarchy

| PDF element | Figma treatment |
|---|---|
| Main title | Bold, 36-44px, with green underline |
| Subtitle | Regular, 16-20px, muted color |
| Section header | Bold, 18-22px |
| Body text | Regular, 14-16px |
| Labels/captions | Regular, 11-13px, gray |
| Big numbers | Bold, 48-100px, accent color |
| Footnotes | Regular, 11px, light gray |

### Placeholder types

| PDF element | Figma placeholder |
|---|---|
| Photo/screenshot | Gray rectangle with rounded corners, shadow |
| Bar chart | Frame with colored rectangles representing bars |
| Line chart | Series of small circles with connecting rectangles |
| Pie chart | Gray circle |
| Table | Frame rows with alternating background |
| QR code | Square frame with border and "QR" label |
| App mockup | Rounded rectangle with basic internal structure |

## Step 2 — Verify

Run a verification script after all slides are created:

```js
const page = figma.getNodeById("TARGET_NODE_ID");
const lines = [];
lines.push("Children: " + page.children.length);
for(const c of page.children){
  lines.push("  " + c.name + " (" + c.width + "x" + c.height + ") at y=" + c.y);
}
print(lines.join("\n"));
figma.notify("Verified");
```

Check:
- Correct number of slides
- All named properly
- All 1920x1080 (or target size)
- Properly spaced vertically

## Example execution

```bash
# Create slides
source venv/bin/activate
python run.py --file /tmp/slide01.js
sleep 4
python run.py --file /tmp/slide02.js
sleep 4
# ... etc

# Or batch with sequential execution:
python run.py --file /tmp/slide01.js && sleep 4 && \
python run.py --file /tmp/slide02.js && sleep 4 && \
python run.py --file /tmp/slide03.js

# Verify
python run.py --file /tmp/verify.js
sleep 3
cat output.txt
```

## Tips

- **Batch sending**: can chain 3-4 slides in one bash command with `&& sleep 4 &&`
- **Reusable patterns**: logo, title+underline, section headers — extract into helper functions within each script
- **Script size**: keep each slide script under 5KB. Complex slides (lots of table rows, many elements) may need splitting
- **Font loading**: load all needed fonts at the top of EVERY script — fonts don't persist between script runs
- **Color consistency**: define brand colors once as variables at script top: `const GREEN = {r:0.243,g:0.812,b:0.557}`
