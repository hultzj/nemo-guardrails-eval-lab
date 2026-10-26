# Step videos

One video per exercise, embedded in the guide (`video::<step>.mp4[]` under each exercise heading,
files in `content/modules/ROOT/assets/images/`). Each is recorded from a real run of the lab as a
lab user, with captions and highlight boxes drawn into the page, waits sped up (with a badge), and
setup cut out. No video is longer than two minutes.

Re-record after a change to a step's commands or expected results. Recording runs the lab for real
on the user's project, in guide order, so start from a fresh tenant (or run `cleanup` first).

## No credentials on camera

* The user logs in once, before recording and off camera (`login.mjs`); recordings reuse that
  session. The session file is a credential: keep it outside the repository, mode 600.
* Every snapshot of visible text (page text and terminal rows) is checked against the user's
  password, the judge key, the Keycloak admin password, the kubeadmin password and the session
  cookies (all held in memory only), and against token-shaped strings. A match discards the video.
* Commands never print tokens: `oc whoami -t` only ever appears inside `$(...)`.

## Record

From a cluster-admin `oc` login (the secret check reads the judge and Keycloak Secrets):

```bash
cd scripts/videos && npm install && npx playwright install chromium
npm --prefix ../.. run build                       # the recorder pastes commands from the built guide
LAB_USER=user1 LAB_PASSWORD_FILE=<file> DOMAIN=apps.<cluster> node login.mjs <state.json>
LAB_USER=user1 LAB_PASSWORD_FILE=<file> KUBEADMIN_PASSWORD_FILE=<file> DOMAIN=apps.<cluster> \
  STATE=<state.json> node record.mjs all             # or one or more step names: node record.mjs list
```

The user's JupyterLab needs the terminal's screen-reader mode on (the recorder reads and highlights
terminal rows through it), a larger font, and notebook windowing off; set them once in
*Settings > Settings Editor* (Terminal: `screenReaderMode`, `fontSize` 16, `theme` dark; Notebook:
`windowingMode` none).

Every highlight must find its target on screen, or the step fails: a recording cannot show a step
that did not produce the guide's expected result.
