"""Draw deterministic, editable SVG figures; optionally render PNG with Chromium."""

import argparse
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESTINATION = ROOT / "docs/figures"
INK = "#262626"
MUTED = "#595959"
BLUE_FILL = "#edf2f7"
HUMAN_FILL = "#f7f3e9"


class Figure:
    def __init__(self, width, height, title, description):
        self.width, self.height = width, height
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title description">',
            f'<title id="title">{escape(title)}</title>',
            f'<desc id="description">{escape(description)}</desc>',
            "<defs>",
            '<marker id="arrow" viewBox="0 0 8 8" refX="7.3" refY="4" '
            'markerWidth="6" markerHeight="6" orient="auto-start-reverse" '
            f'markerUnits="strokeWidth"><path d="M 0 0 L 8 4 L 0 8 Z" fill="{INK}"/></marker>',
            "</defs>",
            '<rect width="100%" height="100%" fill="white"/>',
            '<g font-family="Arial, Helvetica, sans-serif" fill="#262626">',
        ]

    def text(self, x, y, content, *, size=18, weight="normal", anchor="middle", muted=False):
        self.parts.append(
            f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" fill="{MUTED if muted else INK}">{escape(content)}</text>'
        )

    def panel(self, y, label):
        self.text(40, y, label, size=24, weight="bold", anchor="start")

    def box(self, x, y, width, height, title, detail=(), *, kind="code"):
        fill = {"agent": BLUE_FILL, "human": HUMAN_FILL, "code": "white"}[kind]
        self.parts.append(
            f'<g data-box="{escape(title)}">'
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
            f'fill="{fill}" stroke="{INK}" stroke-width="1.3"/>'
        )
        lines = [title] if isinstance(title, str) else title
        content_height = len(lines) * 23 + len(detail) * 21 + (7 if detail else 0)
        baseline = y + (height - content_height) / 2 + 19
        for line in lines:
            self.text(x + width / 2, baseline, line, size=19, weight="bold")
            baseline += 23
        if detail:
            baseline += 7
        for line in detail:
            self.text(x + width / 2, baseline, line, size=15, muted=True)
            baseline += 21
        self.parts.append("</g>")

    def edge(self, points, *, dashed=False, both=False, arrow=True):
        coordinates = " ".join(f"{x},{y}" for x, y in points)
        attributes = 'stroke-dasharray="6 5"' if dashed else ""
        if arrow:
            attributes += ' marker-end="url(#arrow)"'
        if both:
            attributes += ' marker-start="url(#arrow)"'
        self.parts.append(
            f'<polyline points="{coordinates}" fill="none" stroke="{INK}" '
            f'stroke-width="1.45" stroke-linejoin="miter" {attributes}/>'
        )

    def legend(self, y, *, workflow=False):
        self.edge([(40, y - 5), (82, y - 5)])
        self.text(94, y, "Control flow", size=15, anchor="start", muted=True)
        self.edge([(236, y - 5), (278, y - 5)], dashed=True)
        self.text(
            290,
            y,
            "Feedback / resume" if workflow else "Data access / persistence",
            size=15,
            anchor="start",
            muted=True,
        )
        self.text(
            self.width - 40,
            y,
            "Blue: Agent nodes  |  Ochre: human-input node"
            if workflow
            else "Offline scripted model  |  Simulated business actions",
            size=15,
            anchor="end",
            muted=True,
        )

    def svg(self):
        return "\n".join([*self.parts, "</g>", "</svg>", ""])


def architecture():
    figure = Figure(
        1460,
        626,
        "AfterSales system architecture",
        "The workbench submits scoped, idempotent requests to FastAPI and a durable "
        "application service. LangGraph orchestrates read-only Agent tools and approved "
        "transactional actions. Business records and graph checkpoints use separate SQLite files.",
    )
    figure.panel(38, "(a) Service and execution boundaries")
    figure.box(40, 90, 220, 100, "Workbench", ("Customer / operator", "Requests and polling"))
    figure.box(320, 90, 220, 100, "FastAPI", ("Identity scope", "Request validation"))
    figure.box(
        600,
        90,
        240,
        100,
        "Application service",
        ("Durable jobs + idempotency", "Bounded execution workers"),
    )
    figure.box(
        920,
        90,
        480,
        100,
        "LangGraph runtime",
        ("LangChain role-local tool loops", "Interrupt / resume"),
        kind="agent",
    )
    for start, end in [(260, 320), (540, 600), (840, 920)]:
        figure.edge([(start, 140), (end - 3, 140)])

    figure.box(
        600,
        264,
        240,
        94,
        "Scoped read-only tools",
        ("Evidence snapshots", "Deterministic policy rules"),
    )
    figure.box(
        920,
        264,
        480,
        94,
        "Transactional action service",
        ("Approval binding + latest-fact / policy checks", "Atomic effects + idempotency ledger"),
    )
    figure.edge([(1020, 190), (1020, 224), (720, 224), (720, 261)])
    figure.text(827, 214, "Tool calls", size=15, muted=True)
    figure.edge([(1275, 190), (1275, 261)])
    figure.text(1293, 231, "Approved", size=15, anchor="start", muted=True)

    figure.panel(430, "(b) Durable state")
    figure.box(
        40,
        458,
        800,
        94,
        "Business SQLite",
        (
            "Orders / policies / evidence / human inputs / jobs",
            "Run budgets / business effects / action ledger / events",
        ),
    )
    figure.box(
        920,
        458,
        480,
        94,
        "Checkpoint SQLite",
        ("Graph checkpoints", "Workflow / state version manifest"),
    )
    figure.edge([(600, 167), (570, 167), (570, 455)], dashed=True)
    figure.text(556, 396, "Jobs / inputs", size=15, anchor="end", muted=True)
    figure.edge([(720, 358), (720, 455)], dashed=True)
    figure.text(735, 395, "Reads", size=15, anchor="start", muted=True)
    figure.edge([(1190, 358), (1190, 410), (810, 410), (810, 455)], dashed=True)
    figure.text(1055, 400, "Atomic commit", size=15, muted=True)
    figure.edge([(1400, 168), (1424, 168), (1424, 505), (1403, 505)], dashed=True)
    figure.legend(597)
    return figure.svg()


def workflow():
    figure = Figure(
        1460,
        676,
        "Evidence-constrained multi-agent workflow",
        "Order investigation and policy candidate retrieval run independently, followed by a "
        "checked join, facts-dependent policy assessment and a structured proposal. Deterministic "
        "validation and review precede a version-bound human input node. Approved graph execution "
        "revalidates facts before atomic effects. Dashed paths show bounded revision "
        "and missing-information resume.",
    )
    figure.panel(38, "(a) Investigation and proposal")
    figure.box(40, 202, 176, 90, "Coordinator", ("Intake / routing",), kind="agent")
    figure.box(
        292, 100, 240, 84, "Order investigation", ("Order / tracking / proof",), kind="agent"
    )
    figure.box(292, 310, 240, 84, "Policy candidates", ("Retrieval only",), kind="agent")
    figure.text(412, 248, "Independent read branches", size=15, muted=True)
    figure.box(588, 202, 148, 90, "Checked join", ("Facts + evidence",))
    figure.box(790, 202, 230, 90, "Policy assessment", ("Facts-dependent rules",), kind="agent")
    figure.box(1078, 202, 220, 90, "Coordinator", ("Structured proposal",), kind="agent")
    figure.edge([(216, 247), (250, 247)], arrow=False)
    figure.edge([(250, 247), (250, 142), (289, 142)])
    figure.edge([(250, 247), (250, 352), (289, 352)])
    figure.edge([(532, 142), (560, 142), (560, 231), (585, 231)])
    figure.edge([(532, 352), (560, 352), (560, 263), (585, 263)])
    figure.edge([(736, 247), (787, 247)])
    figure.edge([(1020, 247), (1075, 247)])

    figure.panel(457, "(b) Review and approved execution")
    figure.box(1078, 486, 220, 90, "Code validation", ("Facts / references / amounts",))
    figure.box(790, 486, 230, 90, "Review Agent", ("With minimum code review",), kind="agent")
    figure.box(480, 486, 246, 90, "Human-input node", ("Interrupt + bound resume",), kind="human")
    figure.box(40, 486, 246, 90, "Execution node", ("Revalidation + atomic ledger",))
    figure.edge([(1188, 292), (1188, 483)])
    figure.edge([(1078, 531), (1023, 531)])
    figure.edge([(790, 531), (729, 531)])
    figure.edge([(480, 531), (289, 531)])
    figure.text(383, 519, "Approved payload", size=15, muted=True)
    figure.edge([(603, 483), (603, 419), (128, 419), (128, 295)], dashed=True)
    figure.text(347, 410, "Customer information resume", size=15, muted=True)
    figure.edge([(905, 576), (905, 617), (1342, 617), (1342, 247), (1301, 247)], dashed=True)
    figure.text(1125, 607, "Bounded revision", size=15, muted=True)
    figure.legend(652, workflow=True)
    return figure.svg()


def render():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for path in sorted(DESTINATION.glob("*.svg")):
                page = browser.new_page(device_scale_factor=2)
                try:
                    page.set_content(path.read_text())
                    page.add_style_tag(content="html, body { margin: 0; background: white; }")
                    svg = page.locator("svg")
                    width = int(svg.get_attribute("width"))
                    height = int(svg.get_attribute("height"))
                    page.set_viewport_size({"width": width, "height": height})
                    page.evaluate("document.fonts.ready")
                    problems = page.evaluate(
                        """() => [...document.querySelectorAll('svg text')].flatMap(text => {
                          const b = text.getBBox(), svg = text.ownerSVGElement;
                          const box = text.closest('[data-box]')?.querySelector('rect');
                          const x = box ? +box.getAttribute('x') + 8 : 0;
                          const y = box ? +box.getAttribute('y') + 5 : 0;
                          const right = box
                            ? +box.getAttribute('x') + +box.getAttribute('width') - 8
                            : +svg.getAttribute('width');
                          const bottom = box
                            ? +box.getAttribute('y') + +box.getAttribute('height') - 5
                            : +svg.getAttribute('height');
                          const outside = b.x < x || b.y < y || b.x + b.width > right
                            || b.y + b.height > bottom;
                          return outside ? [{text: text.textContent,
                            bounds: {x:b.x,y:b.y,width:b.width,height:b.height}}] : [];
                        })"""
                    )
                    if problems:
                        raise ValueError(f"Text exceeds bounds in {path.name}: {problems}")
                    svg.screenshot(path=str(path.with_suffix(".png")))
                finally:
                    page.close()
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render", action="store_true", help="Render PNGs using installed Chromium"
    )
    parser.add_argument("--check", action="store_true", help="Check committed SVGs against source")
    args = parser.parse_args()
    if args.check and args.render:
        parser.error("--check and --render cannot be combined")
    generated = {"architecture.svg": architecture(), "agent-workflow.svg": workflow()}
    for name, content in generated.items():
        path = DESTINATION / name
        if args.check:
            if path.read_text() != content:
                raise SystemExit(f"Stale figure: {path.relative_to(ROOT)}")
        else:
            DESTINATION.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if args.render:
        render()
    print("README figures: SVG source matches" if args.check else "README figures written")


if __name__ == "__main__":
    main()
