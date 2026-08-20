# pystream agent — local docs

Markdown files in this directory are read by the AI plugin (TXMBot) on
demand. Anything you write here becomes searchable / readable to the model.

How the agent finds things:
- `list_docs()` returns every `.md` file here.
- `search_docs(query)` greps line-by-line across all of them.
- `read_doc("file.md")` returns the full text (truncated at 20 KB).

Conventions that work well:
- One topic per file. Short titles. The model reads filenames before content.
- Lead with concrete facts (PV names, file paths, motor names). The model
  picks these out and quotes them back.
- Update freely — there's no schema and nothing watches the directory.

Files in this directory bootstrap (replace / extend as you like):
- `condensers.md`     — condensers configured at bl32-ID
- `key_pvs.md`        — PV cheatsheet
