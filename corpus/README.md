# University knowledge corpus

Small, deterministic, fictional corpus used to demonstrate the retrieval-augmented
generation pipeline end to end.

**Every fact here is invented for this demo.** No real university, person, address,
phone number or identifier appears in these documents. The corpus exists to prove that a
question is answered from retrieved evidence with visible sources, and that a question
the corpus cannot answer is refused rather than invented.

Documents are Markdown with `#`/`##` headings. Ingestion is deterministic: the same
corpus always produces the same chunks with the same ids and the same vectors.
