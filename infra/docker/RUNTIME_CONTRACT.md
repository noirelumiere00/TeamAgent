# TeamAgent core / media runtime contract

この文書は Terraform・IAM・task definition・CodeBuild を変更せず、それらの担当者へ渡す
deploy contract を定義する。正準な機械可読値は
[`runtime-contract.json`](./runtime-contract.json) と各 Dockerfile の OCI label である。

## Artifact boundary

| task / image | 含めるもの | 含めないもの |
|---|---|---|
| `teamagent-mcp-core` | Python 3.14、TeamAgent core、E5、MCP、DB/AWS client、health、`app.html` | Node/Bun/npm、Playwright/Chromium、ffmpeg、yt-dlp、media worker実装 |
| `teamagent-media-worker` | Python/Node 24、Python/JS Playwright、Chromium、ffmpeg、sanitized yt-dlp、fonts、media contract/operation/worker、TikTok scraper | E5、DB/Slack/OAuth、MCP、Vertex/Anthropic secret、core app |

両方とも `linux/arm64` の独立image・独立taskとし、同一task definitionへ再結合しない。
media workerは1 process / 1 strict jobで終了する。

## Reproducible inputs

Core:

- Chainguard Python builder child:
  `sha256:2eac0b3ef42685b2d45d57633364aaa87ec54bf29960dcf7ecd0eed20e14d124`
- Chainguard Python runtime child:
  `sha256:b7fda4f2d99284fe078f751034a0c858676f3456c4d75f1e935527c1951b5ba9`
- Python `3.14.6`; `/usr/bin/python3.14` SHA-256
  `0d036a463b218cff354adfb9c09a969a9a659698fa376bd3b55fe5bc002e7af8`
- uv child `sha256:9941e2d8e06ff884d328905091eac0a6bc1e40e5ce12e6dd0de4ef4ee26baac4`,
  uv `0.11.29`, binary SHA-256
  `f32f61ced7feb20342032cdac4d0825cebbda61911554f5de5231ec72821812e`
- E5 revision `3d7cfbdacd47fdda877c5cd8a79fbcc4f2a574f3`
- torch `2.12.0+cpu` arm64 wheel SHA-256
  `797c066367792c92eb97cafba7fd0caa8d7455e6078a4ee880630077378dc372`
- baked fallback `app.html` SHA-256
  `716ac25a96516efd6443277c903102d514f3f86729f8706baea41ee48f0ecdeb`

Productionのconnect-webは `CONNECT_APP_HTML_S3_URI` による `source=s3` が正であり、baked
fallbackとQA済みproduction artifactを同一digestとして扱わない。最終core OCI contractは次を
固定する。

- production artifact SHA-256
  `46f0079783cde24b066c7823b7d6672bad12b33debf933a4d7a7ff04b7a3b067`
- S3 VersionId `I1qOb7Kwl.pMg71wqFxbHnbbTqMWjQcY`
- manifest SHA-256
  `15663a838b1bd648443949244c02e66ccfd6cb7b684390baeb1a86efcdd6d4a2`
- build inputs SHA-256
  `1ca6f0213155d8d4dbef4220f641dbb38310fe79473f6c013ef4e54dfa6a87e2`

Image buildはローカルfallbackのbyte hashを検証し、別のOCI labels/provenance filesへ上記S3
artifact contractを記録する。ローカルbuildはS3を取得・更新しない。

Media:

- Alpine-edge Chromium arm64 child:
  `sha256:ee09ed198c66003a3f15024ca4f8f8613b9a97fdfd0dce8600969fc8a69ecc04`
- upstream source:
  `https://github.com/akornatskyy/chromium-headless@04c509a36888b548fa1e88ff30258135ba5a7882`
- Chromium package `150.0.7871.114-r0`, binary SHA-256
  `13eaa3cbe73f39b5feafcd767db0771c4f25d626a3927ba216ac43cde3abaf79`
- Python package `3.14.5-r2`, Node package `24.18.0-r0`, ffmpeg package `8.1.2-r0`
- Python/JavaScript Playwrightはともにexact `1.60.0`
- `media-apk.lock` をactual installed inventoryとbyte compareし、`/lib/apk/db/installed` を保持
- upstream Chromium imageのfilesystemを`scratch`最終stageへbyte copyし、親imageの
  writable `VOLUME /data` metadataを継承しない。最終imageの`Config.Volumes`は`/tmp`だけとする
- yt-dlp `2026.6.9` はwheel/sdist/sanitizer/source-tree hashをOCI labelへ記録する。
  Shahidを含む、actual Trivyでsecret検出された許可外10 extractorのsourceと該当pycを、
  各元source SHA-256を照合して削除する。removed set自体も
  `ea414688b508a2a77bf006e5928536603a51e7ab3b8664c13dd6d21b1140b80b`
  で固定する。lazy extractor一覧でYouTube/TikTok/Instagramが残ることをbuildとsmokeの
  両方で検証する。

Dockerfileの値と異なるoverrideを本番buildへ渡してはならない。すべての外部imageはtagだけでなく
arm64 child digestを指定する。

## Job and lifecycle boundary

Coreは汎用media adapterを介して次を同期的に submit / poll / download / cleanupする。

- `acquire` / `tiktok_acquire`
- `proxy` / `frame` / `thumbnail`
- `slides` / `proposal_pptx` / `pdf`

SQS body相当のenvelopeはPydantic strict/frozen/extra-forbid、最大128 KiBである。入力は
S3 referenceだけを許し、bucket/key/content type/size/SHA-256を必須とする。deadlineは最大15分、
outputは最大128 MiB、idempotency keyとcanonical payload SHA-256を必須とする。URL取得は
HTTPS allowlistとpublic-DNS/redirect SSRF guardを通す。

S3 input/outputはSSE-S3またはSSE-KMS、artifact TTLは5分以上6時間以下とする。workerは成功時に
inputを、失敗時にjob prefix全体を削除する。coreは結果download後に同一job prefixを削除する。
DynamoDB resultにも同じ短期TTLを設定する。

VSEO report/slides/PPTXはrequest単位directoryに置き、upload/response後に
`cleanup_output()` を必ず呼ぶ。既定rootは `/tmp/teamagent/vseo_reports`。
`VIDEO_APPROVAL_STATE_PATH` は
`/tmp/teamagent/state/video_approval_processed.json` が既定であり、task再起動を跨ぐ永続性が
必要になった場合だけ、IaC担当が暗号化された専用mount/object storeを別契約として追加する。

## Runtime and IaC handoff

両taskで以下を強制する。

- UID/GID `10001:10001`
- read-only root filesystem
- fresh named `/tmp` volume、mode `1777`
- `HOME`, `TMPDIR`, `XDG_*` はすべて `/tmp/teamagent/**`
- memory hard limit `4096 MiB`
- writable mountは `/tmp` だけ

Core healthcheckはimageとComposeの双方で
`/app/.venv/bin/python` + `urllib.request` を用いる。curl/wgetを追加しない。

CoreはLinux capabilitiesを`ALL` dropし、`no-new-privileges`を適用する。MediaはChromium
sandboxを有効にしたまま動かすためsetuid sandbox helperを必要とし、
`no-new-privileges`を指定しない。actual ARM64 smokeでは、Docker/ECSの既定capabilitiesのうち
`SYS_CHROOT`だけを保持すればsandboxが成功し、`SYS_CHROOT`もdropするとChromiumが
`sys_chroot`でfail closedすることを確認した。したがってMediaのIaCはcapabilityを追加せず、
`AUDIT_WRITE`, `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `FSETID`, `KILL`, `MKNOD`,
`NET_BIND_SERVICE`, `NET_RAW`, `SETFCAP`, `SETGID`, `SETPCAP`, `SETUID`を明示dropし、
`SYS_CHROOT`だけを保持する。正準リストは`runtime-contract.json`とする。

Playwright
`v1.60.0`の公式seccomp profileをupstream byte SHA-256
`cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`
でvendorする。sourceは
`https://github.com/microsoft/playwright/blob/v1.60.0/utils/docker/seccomp_profile.json`。
default allowlistへuser namespace用の `clone`, `setns`, `unshare` だけを追加する。
`--no-sandbox` とseccomp unconfinedは本番・最終smokeの双方で禁止する。

Media task roleが直接使用するAWS clientはS3とDynamoDBだけである。必要actionとconditional KMS
actionは `runtime-contract.json` に列挙した。`bedrock:*`, `rds:*`, `rds-db:*`,
`secretsmanager:*`, `ssm:*` は付与しない。DB/Slack/OAuth/MCP bearer/Vertex/E5 secretを
environment、secret injection、sidecarのいずれでも渡さない。queue dispatchやtask起動権限は
dispatcher側roleに置き、worker roleへ混ぜない。

## Build, scan, SBOM and provenance gates

ローカル証跡buildは、全変更をcommitしたclean worktreeで次を実行する。

```sh
infra/docker/build_local_runtime_evidence.sh /private/tmp/teamagent-runtime-evidence
```

このscriptはpushを行わず、full 40-character `HEAD` を
`org.opencontainers.image.revision` に設定する。Buildx metadata、image inspect/history、
CycloneDX SBOM、Trivy vulnerability/secret JSON、clean HEADのtracked-files-only source secret
scan、smoke logと全証跡SHA-256を同じdirectoryへ出す。
label revision、architecture、runtime userが一致しない場合は停止する。

採用gateはactual final ARM64 imageごとに以下すべてである。

- Trivy CRITICAL `0`
- Trivy HIGH `0`
- Trivy secret `0`
- suppression、`.trivyignore`、VEX、`--ignore-unfixed` を使わない
- package DBを削除しない
- read-only/non-root/fresh `/tmp` smoke成功

過去HEADやdirty worktreeから作ったimage、brand/catalogだけの判断、base image単体のscanは最終証跡に
使えない。Debian live image、Wolfi Chromium 149候補、Alpine direct-install実験候補も、各時点の
actual scanがgateを満たさなかったため採用根拠にしない。

Registryへpushする権限を持つ別担当は、同じexact HEAD・同じbuild inputsでBuildKit
`provenance=mode=max` とSBOM attestationを付け、attestation subject digestとdeploy digestを
一致させる。ECR basic scan完了後にCRITICAL/HIGHがともに0であることも必須とし、scan未完了・
unsupported・非0ならdeployをfail closedにする。ローカル担当はpush/ECR scan/deployを行わない。

### Fargate Chromium sandbox gate

Playwright公式は非root Chromium sandboxに上記custom seccomp profileを要求する。一方、AWSの
ECS Fargate一次仕様では `dockerSecurityOptions` は非対応で、追加可能capabilityも
`CAP_SYS_PTRACE`だけである。したがってlocal Dockerでのsandbox成功だけからFargate成功を推定
してはならない。IaC担当は最終digestを使った隔離Fargate taskで、`--no-sandbox`なしの
Python/Node Playwright screenshot smokeを実測する。成功するまでmedia taskの本番deployを
fail closedにする。失敗時は機能をOFFにせず、custom seccompを指定できるECS on EC2等の実行基盤を
別途承認・契約する。

## Deterministic smoke

[`compose.runtime-smoke.yml`](./compose.runtime-smoke.yml) は各serviceに別のfresh named `/tmp`
volumeを割り当てる。Coreはoffline E5 1024次元encode、media binary不在、MCP healthを検証する。
Mediaはcontainer networkを`none`にして、Python/Node Playwright route interception、
Chromium screenshot、ffmpeg proxy/frame/thumbnail、slides→PPTX、sanitized yt-dlp allowlistと
deterministic acquire pathを検証する。S3/DynamoDBその他の外部書込は行わない。
