package main

import (
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

func TestGoVersionMustMatchProviderBuild(t *testing.T) {
	if err := checkGoVersion("go1.25.8"); err != nil {
		t.Fatal(err)
	}
	for _, version := range []string{"go1.25.7", "go1.26.0", "devel go1.25.8"} {
		if checkGoVersion(version) == nil {
			t.Fatalf("accepted incompatible Go version %q", version)
		}
	}
}

func TestArchiveMatchesRegressionFixture(t *testing.T) {
	if err := checkGoVersion(runtime.Version()); err != nil {
		t.Fatal(err)
	}
	root := filepath.Join("..", "..")
	source := filepath.Join(root, "infra", "terraform", "lambda", "x_dispatch", "handler.py")
	output := filepath.Join(t.TempDir(), "x_dispatch.zip")
	if err := buildArchive(source, output); err != nil {
		t.Fatal(err)
	}
	actual, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	expected, err := os.ReadFile(filepath.Join(root, "tests", "fixtures", "terraform_archive_2_8_0", "x_dispatch.zip"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(actual, expected) {
		t.Fatal("archive bytes differ from the archive 2.8.0 regression fixture")
	}
}

func TestArchiveIgnoresSourceModeMtimeAndSiblingFiles(t *testing.T) {
	directory := t.TempDir()
	source := filepath.Join(directory, "handler.py")
	if err := os.WriteFile(source, []byte("def handler(event, context):\n    return {}\n"), 0600); err != nil {
		t.Fatal(err)
	}
	first := filepath.Join(directory, "first.zip")
	second := filepath.Join(directory, "second.zip")
	if err := buildArchive(source, first); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(source, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(source, time.Unix(0, 0), time.Unix(0, 0)); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, ".DS_Store"), []byte("noise"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := buildArchive(source, second); err != nil {
		t.Fatal(err)
	}
	one, err := os.ReadFile(first)
	if err != nil {
		t.Fatal(err)
	}
	two, err := os.ReadFile(second)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(one, two) {
		t.Fatal("unlisted files or source metadata changed the archive")
	}
}

func TestArchiveRejectsInvalidSourceAndPreservesExistingOutput(t *testing.T) {
	directory := t.TempDir()
	output := filepath.Join(directory, "output.zip")
	for _, source := range []string{directory, filepath.Join(directory, "missing.py")} {
		if buildArchive(source, output) == nil {
			t.Fatalf("accepted invalid source %q", source)
		}
	}
	source := filepath.Join(directory, "handler.py")
	if err := os.WriteFile(source, []byte("handler"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(output, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	if buildArchive(source, output) == nil {
		t.Fatal("overwrote an existing output")
	}
	content, err := os.ReadFile(output)
	if err != nil || string(content) != "keep" {
		t.Fatalf("existing output changed: %q, %v", content, err)
	}
}
