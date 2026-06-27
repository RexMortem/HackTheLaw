"""3b1b-style narrated explainer for LLM x Law: Pleading-to-Proof.

Five beats, each a Scene that builds itself from the project's REAL data
(case_ui/data/*.json via data_loader). Narration is spoken with manim-voiceover;
animations auto-time to the audio.

Render one beat fast while iterating:
    manim -pql viz/scenes.py OneProposition

Render the whole film (each scene is a separate clip you then stitch):
    manim -pqh viz/scenes.py TheProblem OneProposition TheMatrix TheGraph SinglePointOfFailure

-pql = preview, low quality (fast).  -pqh = high quality.  -qk = 4K final.

No LaTeX required: every label uses Text(), not Tex()/MathTex().
"""
from __future__ import annotations

import numpy as np
from manim import *
from manim_voiceover import VoiceoverScene
from manim_voiceover.services.gtts import GTTSService

import data_loader as D
import theme as T


def v3(p) -> np.ndarray:
    """2D layout point -> manim's 3D coordinate."""
    return np.array([float(p[0]), float(p[1]), 0.0])


class NarratedScene(VoiceoverScene):
    """Base: dark 3b1b backdrop + a zero-config gTTS voice (needs internet).

    For broadcast voices, install manim-voiceover[azure] and swap the service:
        from manim_voiceover.services.azure import AzureService
        self.set_speech_service(AzureService(voice="en-GB-RyanNeural"))
    """

    def setup(self):
        super().setup()
        self.camera.background_color = T.BG
        self.set_speech_service(GTTSService(lang="en", tld="co.uk"))

    def footer(self, text: str) -> Text:
        f = Text(text, font_size=20, color=T.MUTED).to_edge(DOWN, buff=0.3)
        return f


# ---------------------------------------------------------------------------
# 1. The problem
# ---------------------------------------------------------------------------
class TheProblem(NarratedScene):
    def construct(self):
        matrix = D.load_matrix()
        graph = D.load_graph()
        n_ev = len(D.nodes_by_kind(graph, "evidence"))
        n_prop = len(matrix)

        # A drift of "documents" — the unread bundle.
        docs = VGroup(*[
            RoundedRectangle(width=0.34, height=0.46, corner_radius=0.04,
                             stroke_color=T.MUTED, stroke_width=1.2,
                             fill_color=T.EV_FILL, fill_opacity=0.5)
            for _ in range(120)
        ])
        docs.arrange_in_grid(rows=8, cols=15, buff=0.16).scale(0.95)
        docs.set_opacity(0.0)

        with self.voiceover(
            text="A litigation bundle lands on your desk. Dozens of pleaded "
                 "allegations, and hundreds of witness statements and exhibits "
                 "scattered across the case."
        ) as tr:
            self.play(LaggedStart(*[FadeIn(d, shift=DOWN * 0.2) for d in docs],
                                  lag_ratio=0.01, run_time=tr.duration))

        counts = VGroup(
            Text(f"{n_prop}", font_size=64, color=T.INK),
            Text("pleaded allegations & denials", font_size=24, color=T.MUTED),
            Text(f"{n_ev}", font_size=64, color=T.INK),
            Text("pieces of evidence", font_size=24, color=T.MUTED),
        ).arrange_in_grid(rows=2, cols=2, col_buff=0.6, row_buff=0.15)

        with self.voiceover(
            text=f"In this real case: {n_prop} pleaded propositions, and {n_ev} "
                 f"units of evidence that might prove — or disprove — them."
        ) as tr:
            self.play(docs.animate.set_opacity(0.12).scale(1.05), run_time=0.6)
            self.play(FadeIn(counts, scale=0.9),
                      run_time=max(tr.duration - 0.6, 0.4))

        question = Text("Which parts of the case are actually proven?",
                        font_size=40, color=T.INK)
        with self.voiceover(
            text="The hard question is simple to state, and slow to answer by "
                 "hand. Which parts of the case are actually proven?"
        ) as tr:
            self.play(FadeOut(counts), FadeOut(docs), run_time=0.5)
            self.play(Write(question), run_time=max(tr.duration - 0.5, 0.6))
        self.wait(0.3)
        self.play(FadeOut(question))


# ---------------------------------------------------------------------------
# 2. One proposition, mapped to its evidence
# ---------------------------------------------------------------------------
class OneProposition(NarratedScene):
    PID = "P0001"

    def construct(self):
        matrix = D.load_matrix()
        text = D.proposition_text(self.PID, matrix)
        links = D.proposition_links(self.PID, matrix)
        supp = [l for l in links if l["relation"] == "support"][:4]
        adv = [l for l in links if l["relation"] == "undermine"][:3]

        prop_box = RoundedRectangle(width=5.6, height=1.5, corner_radius=0.18,
                                    stroke_color=T.PROP_FILL, stroke_width=2.5,
                                    fill_color="#1a2230", fill_opacity=1.0)
        prop_label = VGroup(
            Text(self.PID, font_size=20, color=T.MUTED),
            Text(D.truncate(text, 64), font_size=20, color=T.INK,
                 line_spacing=0.8).scale_to_fit_width(5.1),
        ).arrange(DOWN, buff=0.12)
        prop = VGroup(prop_box, prop_label).move_to(ORIGIN)

        with self.voiceover(
            text="Take a single pleaded allegation: that the defendant "
                 "defectively designed and built the platform."
        ) as tr:
            self.play(FadeIn(prop_box), Write(prop_label),
                      run_time=tr.duration)

        # Evidence nodes around it.
        def ev_node(lk, angle, color):
            pos = 4.6 * np.array([np.cos(angle), np.sin(angle) * 0.62, 0])
            dot = Dot(point=pos, radius=0.13, color=color)
            lab = Text(D.truncate(lk["quote"] or lk["evidence_id"], 30),
                       font_size=15, color=T.MUTED)
            lab.next_to(dot, RIGHT if pos[0] >= 0 else LEFT, buff=0.12)
            edge = Line(prop_box.get_center(), pos, color=color,
                        stroke_width=2.2, stroke_opacity=0.8)
            edge.set_z_index(-1)
            return VGroup(edge, dot, lab), edge

        right_angles = np.linspace(-0.7, 0.7, max(len(supp), 1))
        left_angles = np.linspace(np.pi - 0.6, np.pi + 0.6, max(len(adv), 1))

        supp_groups = [ev_node(l, a, T.SUPPORT) for l, a in zip(supp, right_angles)]
        adv_groups = [ev_node(l, a, T.UNDERMINE) for l, a in zip(adv, left_angles)]

        with self.voiceover(
            text="The tool retrieves the evidence that bears on it, and asks "
                 "Claude to classify each piece. Green supports the allegation."
        ) as tr:
            self.play(LaggedStart(*[GrowFromPoint(g, prop_box.get_center())
                                    for g, _ in supp_groups],
                                  lag_ratio=0.25, run_time=tr.duration))

        with self.voiceover(
            text="Red undermines it. Here, strong evidence cuts both ways."
        ) as tr:
            self.play(LaggedStart(*[GrowFromPoint(g, prop_box.get_center())
                                    for g, _ in adv_groups],
                                  lag_ratio=0.25, run_time=tr.duration))

        verdict = Text("CONTESTED", font_size=30, color=T.STATUS["contested"],
                       weight=BOLD).next_to(prop, UP, buff=0.45)
        with self.voiceover(
            text="Evidence on both sides, so the status of this allegation is: "
                 "contested. Every cell is auditable back to a verbatim quote."
        ) as tr:
            self.play(prop_box.animate.set_stroke(T.STATUS["contested"]),
                      Write(verdict), run_time=tr.duration)
        self.wait(0.3)
        self.play(*[FadeOut(m) for m in self.mobjects])


# ---------------------------------------------------------------------------
# 3. The whole matrix + trial-readiness score
# ---------------------------------------------------------------------------
class TheMatrix(NarratedScene):
    def construct(self):
        matrix = D.load_matrix()
        statuses = [(item.get("proposition", {}).get("id", "?"),
                     (item.get("status") or "missing").strip().lower())
                    for item in matrix]
        cols = 9

        cells = VGroup(*[
            Square(side_length=0.62, stroke_color=T.BG, stroke_width=2,
                   fill_color=T.MUTED, fill_opacity=0.25)
            for _ in statuses
        ])
        cells.arrange_in_grid(cols=cols, buff=0.12).move_to(ORIGIN).to_edge(LEFT, buff=1.0)

        title = Text("The pleading-to-proof matrix", font_size=30, color=T.INK)
        title.to_edge(UP, buff=0.6)

        with self.voiceover(
            text="Do that for every pleaded allegation, and the whole case "
                 "becomes a single matrix."
        ) as tr:
            self.play(Write(title), FadeIn(cells, lag_ratio=0.01),
                      run_time=tr.duration)

        with self.voiceover(
            text="Each cell lights up by status: supported in green, contested "
                 "in amber, undermined in red, and the dangerous one — no "
                 "evidence at all — in grey."
        ) as tr:
            self.play(LaggedStart(*[
                c.animate.set_fill(T.status_color(st), opacity=0.95)
                for c, (_, st) in zip(cells, statuses)
            ], lag_ratio=0.03, run_time=tr.duration))

        # Legend.
        legend = VGroup()
        for name in ["supported", "contested", "undermined", "missing"]:
            swatch = Square(side_length=0.26, fill_color=T.STATUS[name],
                            fill_opacity=1, stroke_width=0)
            lab = Text(name, font_size=20, color=T.MUTED).next_to(swatch, RIGHT, buff=0.15)
            legend.add(VGroup(swatch, lab))
        legend.arrange(DOWN, aligned_edge=LEFT, buff=0.22)
        legend.to_edge(RIGHT, buff=1.0).shift(UP * 1.2)

        # Trial-readiness score.
        score = D.trial_readiness(matrix)
        tracker = ValueTracker(0.0)
        pct = always_redraw(lambda: Text(
            f"{tracker.get_value() * 100:.0f}%", font_size=72, color=T.INK
        ).next_to(legend, DOWN, buff=0.7))
        score_cap = Text("trial readiness", font_size=22, color=T.MUTED)

        with self.voiceover(
            text="A header score rolls the whole bundle into one number — "
                 "supported counts full, contested counts half. This case sits "
                 f"at {round(score * 100)} percent trial-ready."
        ) as tr:
            self.play(FadeIn(legend), run_time=0.6)
            score_cap.next_to(pct, DOWN, buff=0.1)
            self.add(pct)
            self.play(tracker.animate.set_value(score),
                      FadeIn(score_cap, shift=UP * 0.2),
                      run_time=max(tr.duration - 0.6, 0.8))
        self.wait(0.4)
        self.play(*[FadeOut(m) for m in self.mobjects])


# ---------------------------------------------------------------------------
# 4. The case graph blooms open
# ---------------------------------------------------------------------------
class TheGraph(NarratedScene):
    MAX_EDGES = 300  # cap for legibility/render speed; logged below

    def construct(self):
        graph = D.load_graph()
        idx = D.node_index(graph)

        edges = D.signal_edges(graph)
        edges = sorted(edges, key=lambda e: e.get("confidence", 0), reverse=True)
        capped = edges[: self.MAX_EDGES]
        if len(edges) > self.MAX_EDGES:
            print(f"[TheGraph] drawing {self.MAX_EDGES} of {len(edges)} "
                  f"signal edges (strongest by confidence) for legibility.")

        used = set()
        for e in capped:
            used.add(e["source"])
            used.add(e["target"])
        node_ids = [nid for nid in used if nid in idx]
        edge_pairs = [(e["source"], e["target"]) for e in capped]

        pos = D.spring_layout(node_ids, edge_pairs)

        dots = {}
        for nid in node_ids:
            n = idx[nid]
            is_prop = n.get("kind") == "proposition"
            dots[nid] = Dot(
                point=v3(pos[nid]),
                radius=0.11 if is_prop else 0.05,
                color=T.PROP_FILL if is_prop else T.EV_FILL,
            )
        prop_dots = [d for nid, d in dots.items() if idx[nid].get("kind") == "proposition"]
        ev_dots = [d for nid, d in dots.items() if idx[nid].get("kind") != "proposition"]

        edge_lines = VGroup(*[
            Line(v3(pos[e["source"]]), v3(pos[e["target"]]),
                 color=(T.SUPPORT if e["relation"] == "SUPPORTS" else T.UNDERMINE),
                 stroke_width=1.4, stroke_opacity=0.5)
            for e in capped if e["source"] in pos and e["target"] in pos
        ])
        edge_lines.set_z_index(-1)

        title = Text("The case as a graph", font_size=30, color=T.INK).to_edge(UP, buff=0.5)

        with self.voiceover(
            text="The matrix is really a graph: every allegation wired to the "
                 "evidence that touches it."
        ) as tr:
            self.play(Write(title), run_time=0.6)
            self.play(
                LaggedStart(*[GrowFromCenter(d) for d in ev_dots],
                            lag_ratio=0.003),
                run_time=max(tr.duration - 0.6, 0.8),
            )

        with self.voiceover(
            text="The propositions are the bright spine; the evidence is the "
                 "cloud around them. Green threads support, red threads "
                 "undermine."
        ) as tr:
            self.play(FadeIn(edge_lines), run_time=tr.duration * 0.5)
            self.play(LaggedStart(*[GrowFromCenter(d) for d in prop_dots],
                                  lag_ratio=0.02),
                      run_time=tr.duration * 0.5)
        self.wait(0.4)
        self.play(*[FadeOut(m) for m in self.mobjects])


# ---------------------------------------------------------------------------
# 5. The punchline: a single point of failure
# ---------------------------------------------------------------------------
class SinglePointOfFailure(NarratedScene):
    def construct(self):
        graph = D.load_graph()
        idx = D.node_index(graph)
        center = D.best_articulation_proposition(graph)
        cid = center["id"]

        node_ids, pairs = D.ego_subgraph(cid, graph, hops=2, cap=24)
        if cid not in node_ids:
            node_ids.append(cid)
        pos = D.spring_layout(node_ids, pairs, seed=3, scale=(9.5, 4.8))

        def make_dot(nid):
            n = idx.get(nid, {"kind": "evidence"})
            is_prop = n.get("kind") == "proposition"
            return Dot(point=v3(pos[nid]),
                       radius=0.12 if is_prop else 0.07,
                       color=T.PROP_FILL if is_prop else T.EV_FILL)

        dots = {nid: make_dot(nid) for nid in node_ids}
        lines = {}
        for a, b in pairs:
            if a in pos and b in pos:
                lines[(a, b)] = Line(v3(pos[a]), v3(pos[b]),
                                     color=T.NEUTRAL, stroke_width=1.6,
                                     stroke_opacity=0.7).set_z_index(-1)

        title = Text("Where does the case break?", font_size=30,
                     color=T.INK).to_edge(UP, buff=0.5)

        with self.voiceover(
            text="Now the graph earns its keep. Look at one cluster of the case, "
                 "held together through a single allegation."
        ) as tr:
            self.play(Write(title), run_time=0.5)
            self.play(
                LaggedStart(*[GrowFromCenter(d) for d in dots.values()],
                            lag_ratio=0.04),
                *[Create(l) for l in lines.values()],
                run_time=max(tr.duration - 0.5, 0.8),
            )

        # Highlight the articulation node.
        halo = Circle(radius=0.34, color=T.ACCENT, stroke_width=4).move_to(
            dots[cid].get_center())
        tag = VGroup(
            Text(cid, font_size=18, color=T.ACCENT),
            Text("single point of failure", font_size=18, color=T.ACCENT),
        ).arrange(DOWN, buff=0.06).next_to(halo, UP, buff=0.2)

        with self.voiceover(
            text="The graph flags it as an articulation point — remove it, and "
                 "the case does not just get weaker. It splits."
        ) as tr:
            self.play(Create(halo),
                      dots[cid].animate.set_color(T.ACCENT).scale(1.4),
                      FadeIn(tag), run_time=tr.duration)

        # Remove it and recompute components.
        remaining = [n for n in node_ids if n != cid]
        rem_pairs = [(a, b) for (a, b) in pairs if a != cid and b != cid]
        comps = D.connected_components(remaining, rem_pairs)
        comps = sorted(comps, key=len, reverse=True)

        anims = [FadeOut(dots[cid]), FadeOut(halo)]
        for (a, b), l in lines.items():
            if a == cid or b == cid:
                anims.append(FadeOut(l))

        # Drift the components apart so the fragmentation is visible.
        n_comp = len(comps)
        offsets = {}
        for i, comp in enumerate(comps):
            angle = 2 * np.pi * i / max(n_comp, 1)
            shift = 1.6 * np.array([np.cos(angle), np.sin(angle) * 0.6, 0]) if n_comp > 1 else np.zeros(3)
            for nid in comp:
                offsets[nid] = shift

        with self.voiceover(
            text=f"Pull that one allegation, and what was one connected case "
                 f"breaks into {n_comp} disconnected fragments — issues that no "
                 f"longer hang together. That is exactly the weakness you want "
                 f"to find before opposing counsel does."
        ) as tr:
            self.play(*anims, run_time=min(0.8, tr.duration * 0.3))
            drift = []
            for nid in remaining:
                drift.append(dots[nid].animate.shift(offsets.get(nid, np.zeros(3))))
            for (a, b), l in lines.items():
                if a != cid and b != cid:
                    drift.append(l.animate.shift(
                        (offsets.get(a, np.zeros(3)) + offsets.get(b, np.zeros(3))) / 2))
            self.play(*drift, run_time=max(tr.duration * 0.7, 1.0))

        closing = Text("Stress-test the case theory — before trial.",
                       font_size=30, color=T.INK).to_edge(DOWN, buff=0.8)
        self.play(Write(closing))
        self.wait(0.5)
        self.play(*[FadeOut(m) for m in self.mobjects])
