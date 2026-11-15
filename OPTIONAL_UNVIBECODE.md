# Optional UnvibeCode CLI integration

ContextLens itself does not call a paid AI API and does not require AI credits to launch, import an existing UnvibeCode ZIP, search chunks, inspect relationships, build context exports, or save investigations.

To run a fresh review from a GitHub repository inside ContextLens, install the UnvibeCode CLI separately in the same Python environment:

```bash
python -m pip install --upgrade unvibecode
python -m unvibecode --help
```

Then use **Analyze a repository** in the website. Whether the CLI's own review flow requires external credentials, provider quotas, or paid usage depends on the installed UnvibeCode version and its configuration; ContextLens cannot promise that the upstream analysis itself is credit-free. Existing context ZIP import remains available without the CLI.
