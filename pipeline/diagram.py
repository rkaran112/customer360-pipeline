"""
DIAGRAM - draws the pipeline as a picture, with the REAL numbers from the last run.

It writes Graphviz "DOT" text (a tiny language for boxes and arrows). The dashboard
hands it to st.graphviz_chart, which draws it in the browser - nothing to install.

    sources -> 1 Extract -> 2 Standardize -> 3 Quality gate -> 4 Merge -> 5 Insights -> 6 Checks -> outputs
                                                   |
                                                   v
                                              Quarantine
"""

# Colours for the boxes. >>> TO CHANGE: restyle the diagram here.
COLOURS = {"source": "#E3F2FD", "stage": "#E8F5E9", "bad": "#FFEBEE", "output": "#FFF8E1", "fail": "#FFCDD2"}


def _node(node_id, label, colour, shape="box", width=None):
    """One box in the picture. \\n inside the label = a new line in the box."""
    label = label.replace('"', "'")
    size = f", width={width}" if width else ""
    return f'  {node_id} [label="{label}", shape={shape}, style="rounded,filled", fillcolor="{colour}"{size}];'


def build_dot(run_metrics, dq_report, config):
    """Return the DOT text for the pipeline picture."""
    stages = {m["stage"]: m for m in run_metrics["stages"]}
    lines = ["digraph pipeline {",
             # TB = top to bottom: sources on top, outputs at the bottom (readable on a projector).
             '  rankdir=TB; nodesep=0.5; ranksep=0.35;',
             '  node [fontname="Helvetica", fontsize=12, width=3.6]; edge [fontname="Helvetica", fontsize=11, color="#607D8B"];']

    # Source files (left side), one box each, with how many records each sent.
    for source in config["input_sources"]:
        by = dq_report["by_source"].get(source["name"], {})
        lines.append(_node(f"src_{source['name']}",
                           f"{source['file']}\\n{source.get('system', '')} ({source['format'].upper()})\\n"
                           f"{by.get('total', 0)} records", COLOURS["source"], "folder", width=2.2))
        lines.append(f'  src_{source["name"]} -> s1 [label="{by.get("total", 0)}"];')

    # The 6 stages in a row. Each box: name, what it does, rows in -> out, time taken.
    order = list(stages)
    for i, name in enumerate(order, start=1):
        m = stages[name]
        colour = COLOURS["stage"] if m.get("ok", True) else COLOURS["fail"]
        lines.append(_node(f"s{i}", f"{i}. {name}\\n{m['description']}\\n"
                                    f"in {m['rows_in']}  ->  out {m['rows_out']}\\n{m['duration_ms']} ms", colour))
        if i > 1:
            lines.append(f'  s{i-1} -> s{i} [label="{stages[order[i-2]]["rows_out"]}"];')

    # Quarantine branch hanging off the quality gate.
    failures = "\\n".join(f"{k}: {v}" for k in
                          ["duplicates", "invalid_emails", "missing_fields", "invalid_customer_ids", "negative_revenue"]
                          if (v := dq_report.get(k, 0)))
    q = dq_report.get("quarantined_records", 0)
    lines.append(_node("quarantine", f"Quarantine\\n{q} records kept with a reason\\n{failures}", COLOURS["bad"], "note"))
    lines.append(f'  s3 -> quarantine [label="{q} rejected", color="#E53935", fontcolor="#E53935"];')

    # Outputs (right side).
    lines.append(_node("out", "Outputs\\ncustomer_360 csv/json/parquet\\ndq_report.json, insights/*.csv\\nDashboard",
                       COLOURS["output"], "box3d"))
    lines.append(f'  s{len(order)} -> out;')
    lines.append("}")
    return "\n".join(lines)
