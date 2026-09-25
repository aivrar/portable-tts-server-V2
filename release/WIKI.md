# Publishing the GitHub wiki

The user manual in `manual/` is the source for the public [GitHub wiki](https://github.com/aivrar/portable-tts-server-V2/wiki). The wiki also includes the portability guide, build instructions, screenshot notes, a home page, sidebar, footer, and all nine PNG images.

Create the first Home page on GitHub once so its separate Git repository exists. Clone it into ignored output storage:

```powershell
git clone https://github.com/aivrar/portable-tts-server-V2.wiki.git output/github-wiki
python tools/sync_wiki.py --output output/github-wiki
```

The renderer checks the destination remote and that every manual page has a wiki page mapping. It changes relative links to wiki URLs, copies screenshots into the wiki, and links remaining source files to the main repository. It does not commit, push, or delete unrelated wiki pages. Generated pages are overwritten from source, so preserve useful online edits in the corresponding source documents before rendering again.

Review the output before publishing:

```powershell
git -C output/github-wiki diff --stat
git -C output/github-wiki add -- .
git -C output/github-wiki diff --cached --check
git -C output/github-wiki commit -m "Update the illustrated TTS manual"
git -C output/github-wiki push origin master
```

For later updates, first commit or preserve local wiki edits, then fetch and fast-forward the wiki checkout. Update and publish the source documents too. Review generated links, heading fragments, image loading, and the public Home/Quickstart pages after pushing. The wiki's default branch is currently `master`; the app repository uses `main`.

This documentation workflow does not rebuild or replace an existing downloadable release.
