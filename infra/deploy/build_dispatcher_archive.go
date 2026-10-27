// Build the source_file ZIP used by hashicorp/archive 2.8.0 without Terraform.
// ArchiveFile reference: https://github.com/hashicorp/terraform-provider-archive/blob/v2.8.0/internal/provider/zip_archiver.go
package main

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"time"
)

const archiveGoVersion = "go1.25.8"

func checkGoVersion(version string) error {
	// flate output can change between Go releases; match the locked provider build.
	if version != archiveGoVersion {
		return fmt.Errorf("requires %s (archive provider 2.8.0 build), got %s", archiveGoVersion, version)
	}
	return nil
}

func buildArchive(source, output string) (err error) {
	info, err := os.Stat(source)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return errors.New("source-file must be a regular file")
	}
	content, err := os.ReadFile(source)
	if err != nil {
		return err
	}
	header, err := zip.FileInfoHeader(info)
	if err != nil {
		return err
	}
	header.Name = filepath.ToSlash(info.Name())
	header.Method = zip.Deflate
	header.SetModTime(time.Time{})
	header.SetMode(0644)

	file, err := os.OpenFile(output, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	defer func() {
		if closeErr := file.Close(); err == nil {
			err = closeErr
		}
		if err != nil {
			os.Remove(output)
		}
	}()
	writer := zip.NewWriter(file)
	entry, err := writer.CreateHeader(header)
	if err != nil {
		return err
	}
	if _, err = entry.Write(content); err != nil {
		return err
	}
	return writer.Close()
}

func main() {
	source := flag.String("source-file", "", "the archive_file source_file (handler.py)")
	output := flag.String("output", "", "new ZIP path; parent directory must already exist")
	flag.Parse()
	if err := checkGoVersion(runtime.Version()); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if *source == "" || *output == "" || flag.NArg() != 0 {
		flag.Usage()
		os.Exit(2)
	}
	if err := buildArchive(*source, *output); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	data, err := os.ReadFile(*output)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	digest := sha256.Sum256(data)
	if err := json.NewEncoder(os.Stdout).Encode(map[string]string{
		"output_path":         *output,
		"output_base64sha256": base64.StdEncoding.EncodeToString(digest[:]),
	}); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
