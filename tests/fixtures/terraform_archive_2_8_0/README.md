# archive 2.8.0 の ZIP 回帰 fixture

`x_dispatch.zip` は、2026-10-27 に既存の activation worktree の
`infra/terraform/build/x_dispatch.zip` からコピーしたローカル artifact。
SHA256（Base64）は `eONRY7AJKh4sc2llELhKLXJ4ZcZAe8+t6ahb7EJTlmw=`。
中の `handler.py` は、この変更の基点の tracked source と一致する。

provider の新規実行結果としての fixture ではない。今回の sandbox は provider の
Unix socket bind を禁止し、Go も未導入のため、今回 provider 実物との再比較はしていない。
Go self-test はこの既存 artifact と出力 ZIP の byte 一致を検証する。

根拠は locked `hashicorp/archive` 2.8.0 の
[ZipArchiver.ArchiveFile](https://github.com/hashicorp/terraform-provider-archive/blob/v2.8.0/internal/provider/zip_archiver.go)。
`zip.FileInfoHeader` → basename → Deflate → `SetModTime(time.Time{})` →
`SetMode(0644)` → `CreateHeader` → `Write` → `Close` を再現する。
ゼロ日時はこの ZIP では DOS 日時 2049-01-01 として見える。Python の 1980-01-01 指定と違う。

cached darwin_arm64 `terraform-provider-archive_v2.8.0_x5` の Go build 情報は
`go1.25.8`。圧縮実装の差を避けるため builder は同じ Go patch version に固定する。
provider または Go を変える場合、provider が作る同じ `source_file`・`output_file_mode`
の ZIP と builder の ZIP を byte 比較してから pin と fixture を更新する。

本番操作前には guard が作る plan の archive hash と builder の `output_base64sha256`
を比較する。一致しなければ Lambda 更新・state 同期を止める。
