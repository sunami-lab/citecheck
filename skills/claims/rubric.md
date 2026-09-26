# Does the cited work support the sentence?

Judge each pair of a citing sentence and the cited work's abstract. Judge only the part of the sentence attributed to this citation: in "X [a] and Y [b]", reference `a` is responsible for X, not Y.

| Label | Use when |
|---|---|
| SUPPORTED | The abstract states the attributed claim, or the claim follows directly from it: the sentence names the method, model, dataset, task or finding that the abstract describes. |
| PLAUSIBLE | The work is on the right topic, and the attributed detail (a number, an implementation choice, a secondary result) is the kind of thing its full text would contain, but the abstract does not state it. |
| UNSUPPORTED | The abstract is about a different problem, method or finding than the one the sentence attributes to it, so the citation looks like the wrong paper for this sentence. |
| CONTRADICTED | The abstract states the opposite of the attributed claim. |
| UNCHECKED | There is no abstract, or the sentence attributes nothing specific to the citation. |

- Background citations ("diffusion models [a] are widely used") are SUPPORTED when the work is about that topic.
- A tool, dataset or model cited for its use ("we train with Adam [a]") is SUPPORTED when the work introduces it.
- Survey or textbook citations are SUPPORTED for claims within their scope.
- When unsure between PLAUSIBLE and UNSUPPORTED, choose PLAUSIBLE. A false alarm costs the author time, and an abstract is only a summary of the paper.
- Give a reason in one short sentence that names what the abstract says the work does.
