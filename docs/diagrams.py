"""Render the design-document diagrams to PNG (python docs/diagrams.py)."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

OUT = Path(__file__).parent / "img"

BLUE = "#dbe9f7"
GREEN = "#dff3e3"
ORANGE = "#fde9d3"
PURPLE = "#ece3f6"
GREY = "#f1f1f1"
YELLOW = "#fff6cc"
EDGE = "#3b4a5a"


def box(ax, x, y, w, h, text, color=BLUE, size=8.5, bold_first=True, style="round,pad=0.02,rounding_size=0.8", lw=1.1, ls="-"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=style, fc=color, ec=EDGE, lw=lw, ls=ls))
    lines = text.split("\n")
    if bold_first:
        ax.text(x + w / 2, y + h / 2 + (len(lines) - 1) * 1.05, lines[0], ha="center", va="center", fontsize=size, weight="bold")
        for i, line in enumerate(lines[1:], start=1):
            ax.text(x + w / 2, y + h / 2 + (len(lines) - 1) * 1.05 - i * 2.1, line, ha="center", va="center", fontsize=size - 1.2)
    else:
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size)


def arrow(ax, p1, p2, label="", both=False, dashed=False, lx=0.0, ly=0.0, size=7.2, color=EDGE, rad=0.0):
    ax.add_patch(
        FancyArrowPatch(p1, p2, arrowstyle="<|-|>" if both else "-|>", mutation_scale=10, lw=1.1, color=color,
                        ls=(0, (4, 3)) if dashed else "-", connectionstyle=f"arc3,rad={rad}")
    )
    if label:
        mx, my = (p1[0] + p2[0]) / 2 + lx, (p1[1] + p2[1]) / 2 + ly
        ax.text(mx, my, label, fontsize=size, ha="center", va="center", color="#1d2733",
                bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.9))


def canvas(w, h, xmax, ymax):
    fig, ax = plt.subplots(figsize=(w, h), dpi=200)
    ax.set_xlim(0, xmax)
    ax.set_ylim(0, ymax)
    ax.axis("off")
    return fig, ax


def container_diagram() -> None:
    fig, ax = canvas(10, 5.6, 120, 67)
    box(ax, 1, 50, 14, 10, "Source systems\nmonitoring, batch,\nintegrations", GREY)
    box(ax, 1, 8, 14, 10, "Operators\nVIEWER /\nENGINEER / ADMIN", GREY)
    box(ax, 20, 8, 13, 52, "API Gateway\n(Azure APIM)\n\nAPI-key check\nJWT validation\nrate limiting\nrouting\nsize limits", PURPLE)

    box(ax, 40, 50, 19, 10, "Ingestion Service\nvalidation, API key,\nidempotency, dedup", BLUE)
    box(ax, 63, 51.5, 17, 8, "ingestion DB\nraw alerts 7d, outbox", YELLOW, size=8)
    box(ax, 40, 8, 19, 10, "Incident Service\nsystem of record,\nlifecycle + audit", BLUE)
    box(ax, 40, 22.5, 19, 8.5, "incidents DB\nincidents, audit,\nsuggestions, outbox", YELLOW, size=8)

    box(ax, 68, 29, 22, 10, "Message broker\nRabbitMQ (local) /\nAzure Service Bus", ORANGE)
    box(ax, 96, 8, 23, 10, "Triage Agent Service\ntool loop (max 4 steps),\nrunbooks + owners (JSON)", GREEN)
    box(ax, 96, 30, 23, 8, "LLM endpoint\nAzure OpenAI / mock", GREY)
    box(ax, 96, 51, 23, 9, "Notification Service\n(design only)\nTeams / Slack / email", GREY, ls="--")

    arrow(ax, (15, 55), (20, 55), "")
    ax.text(17.5, 61.5, "HTTPS +\nX-API-Key", fontsize=7, ha="center")
    arrow(ax, (15, 13), (20, 13), "")
    ax.text(17.5, 5.2, "HTTPS +\nJWT", fontsize=7, ha="center")
    arrow(ax, (33, 55), (40, 55), "POST\n/alerts", ly=3.2)
    arrow(ax, (33, 13), (40, 13), "REST", ly=2.2)
    arrow(ax, (59, 55.5), (63, 55.5))
    arrow(ax, (49.5, 18), (49.5, 22.5), both=True)

    arrow(ax, (57, 50), (73, 39), "AlertReceived", lx=1, ly=1.5)
    arrow(ax, (72, 29), (57, 18), "in: AlertReceived,\nTriageCompleted\nout: IncidentCreated", both=True, lx=3.5, ly=-0.5)
    arrow(ax, (88, 29), (101, 18), "in: IncidentCreated\nout: TriageCompleted", both=True, lx=3.0, ly=1.5)
    arrow(ax, (107.5, 18), (107.5, 30), "redacted\nprompt", lx=6.2)
    arrow(ax, (90, 36), (100, 51), "IncidentCreated,\nTriageCompleted", dashed=True, lx=-5, ly=2)
    arrow(ax, (96, 11), (59, 11), "GET /internal/v1/*  (read-only service token)", dashed=True, ly=-2.4)
    ax.text(60, 65, "Container view - each service owns its data; services talk via events, the agent only reads",
            ha="center", fontsize=9.5, weight="bold")
    fig.savefig(OUT / "container.png", bbox_inches="tight")
    plt.close(fig)


def sequence_diagram() -> None:
    actors = ["Source", "Ingestion", "Broker", "Incident", "Triage Agent", "LLM", "Engineer"]
    fig, ax = canvas(10, 5.4, 140, 78)
    xs = [8 + i * 20.5 for i in range(len(actors))]
    for x, name in zip(xs, actors, strict=True):
        box(ax, x - 8, 70, 16, 6, name, GREY if name in ("Source", "Engineer", "LLM") else BLUE, size=8.5, bold_first=False)
        ax.plot([x, x], [2, 70], color="#9aa5b1", lw=0.9, ls=(0, (3, 3)))

    steps = [
        (0, 1, "1 POST /alerts (API key, Idempotency-Key)"),
        (1, 0, "2 202 Accepted {alert_id}  [alert + outbox in 1 tx]"),
        (1, 2, "3 relay: AlertReceived"),
        (2, 3, "4 AlertReceived"),
        (3, 3, "5 dedup by fingerprint; incident + audit + outbox (1 tx)"),
        (3, 2, "6 relay: IncidentCreated"),
        (2, 4, "7 IncidentCreated"),
        (4, 3, "8 tools: GET incident / similar (service token)"),
        (4, 5, "9 redacted prompt + tool results (<= 4 steps)"),
        (5, 4, "10 structured JSON"),
        (4, 2, "11 validate + ground -> TriageCompleted"),
        (2, 3, "12 TriageCompleted -> suggestion PENDING_REVIEW"),
        (6, 3, "13 GET incident; POST decision ACCEPT/REJECT (JWT)"),
    ]
    y = 65
    for a, b, label in steps:
        if a == b:
            ax.add_patch(FancyBboxPatch((xs[a] - 0.8, y - 2.6), 1.6, 4.2, boxstyle="square,pad=0", fc=BLUE, ec=EDGE, lw=0.9))
            ax.text(xs[a] + 2, y - 0.4, label, fontsize=7, va="center")
            y -= 5
            continue
        dashed = label.startswith(("2 ", "10 "))
        ax.add_patch(FancyArrowPatch((xs[a], y), (xs[b], y), arrowstyle="-|>", mutation_scale=9, color=EDGE, lw=1,
                                     ls=(0, (4, 3)) if dashed else "-"))
        ax.text(min(xs[a], xs[b]) + 1, y + 1.3, label, fontsize=7, ha="left")
        y -= 5
    fig.savefig(OUT / "sequence.png", bbox_inches="tight")
    plt.close(fig)


def azure_diagram() -> None:
    fig, ax = canvas(10, 6.0, 120, 72)
    box(ax, 1, 52, 14, 9, "Source systems", GREY, bold_first=False)
    box(ax, 1, 34, 14, 9, "Operators\n(Entra ID SSO)", GREY)

    # VNet
    ax.add_patch(FancyBboxPatch((19, 3), 76, 64, boxstyle="round,pad=0.2,rounding_size=1.5", fc="#f7fbff", ec="#5b7fa6", lw=1.4, ls="--"))
    ax.text(21, 64.5, "Azure VNet (spoke) - no public endpoints except App Gateway", fontsize=8, color="#2e5077", weight="bold")
    box(ax, 22, 43, 14, 16, "App Gateway\nWAF v2\n(public IP,\nTLS)", PURPLE)
    box(ax, 40, 43, 15, 16, "API Management\n(internal VNet)\nvalidate-jwt\nsubscription keys\nrate-limit-by-key", PURPLE)

    ax.add_patch(FancyBboxPatch((59, 33), 34, 30, boxstyle="round,pad=0.2,rounding_size=1.2", fc="#eef6ee", ec="#4f8a5b", lw=1.2))
    ax.text(60.5, 60.5, "Container Apps env (internal)", fontsize=7.6, color="#2f6b3c", weight="bold")
    box(ax, 61, 49, 14, 8, "ingestion\nHTTP scale 2-10", BLUE, size=8)
    box(ax, 77, 49, 14, 8, "incident\nHTTP scale 2-10", BLUE, size=8)
    box(ax, 69, 36, 16, 8, "triage-agent\nKEDA (queue) 1-5", GREEN, size=8)

    ax.add_patch(FancyBboxPatch((22, 6), 71, 22, boxstyle="round,pad=0.2,rounding_size=1.2", fc="#fffaf0", ec="#b38b3c", lw=1.2))
    ax.text(24, 25.5, "Private endpoints subnet (public network access disabled)", fontsize=7.6, color="#7a5a17", weight="bold")
    box(ax, 24, 9, 15, 13, "PostgreSQL\nFlexible Server\nzone-redundant HA\nDB per service", YELLOW, size=8)
    box(ax, 41, 9, 15, 13, "Service Bus\nPremium\ntopics + subs\nDLQ, dup-detect", ORANGE, size=8)
    box(ax, 58, 9, 15, 13, "Key Vault\nsource API keys,\n3rd-party secrets", YELLOW, size=8)
    box(ax, 75, 9, 16, 13, "Azure OpenAI\n(private endpoint,\nEntra auth,\nno API key)", GREY, size=8)

    box(ax, 99, 52, 20, 9, "Entra ID\nusers, app roles,\nmanaged identities", GREY, size=8)
    box(ax, 99, 38, 20, 9, "App Insights +\nLog Analytics\nalerts, workbooks", GREY, size=8)
    box(ax, 99, 24, 20, 9, "Azure Container\nRegistry", GREY, size=8)
    box(ax, 99, 8, 20, 10, "GitHub Actions\nOIDC -> ACR,\nBicep, revisions", GREY, size=8)

    arrow(ax, (15, 56.5), (22, 54), "HTTPS", ly=2)
    arrow(ax, (15, 38.5), (22, 47), "HTTPS\n+ JWT", lx=1.5, ly=3.5)
    arrow(ax, (36, 51), (40, 51))
    arrow(ax, (55, 53), (61, 53))
    arrow(ax, (55, 47), (77, 50.5), rad=-0.15)
    arrow(ax, (84, 49), (80, 44), both=True, dashed=True)
    arrow(ax, (68, 49), (48.5, 22), "events", lx=-3)
    arrow(ax, (80, 36), (82, 22), "MI token", lx=6, ly=3)
    arrow(ax, (64, 33), (31.5, 22), "Entra DB auth", lx=-6, ly=2)
    arrow(ax, (93, 45), (99, 42), "OTel", ly=2)
    arrow(ax, (109, 24), (109, 18), both=True)
    arrow(ax, (99, 29), (93, 38), "pull (MI)", lx=3.5, ly=-1)
    ax.text(60, 70.5, "Azure reference deployment (Option 1)", ha="center", fontsize=9.5, weight="bold")
    fig.savefig(OUT / "azure.png", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    container_diagram()
    sequence_diagram()
    azure_diagram()
    print("diagrams written to", OUT)
