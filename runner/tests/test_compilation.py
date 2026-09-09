from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import post_job, require_clang


def test_normalizes_compiler_diagnostic_to_source_range(client: TestClient) -> None:
    require_clang()
    response = post_job(
        client,
        {
            "schema_version": "1.0",
            "request_id": "diagnostic",
            "profile_id": "cpp-clang-c++20-single",
            "action": "compile_and_run",
            "files": [
                {
                    "path": "src/main.cpp",
                    "content": "int main() {\n  return missing_name;\n}\n",
                }
            ],
            "stdin": "",
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "COMPILE_ERROR"
    assert result["execution"] is None
    diagnostic = next(
        item for item in result["diagnostics"] if item["severity"] == "error"
    )
    assert diagnostic["file"] == "src/main.cpp"
    assert diagnostic["range"]["start_line"] == 2
    assert diagnostic["range"]["start_column"] > 1
    assert "missing_name" in diagnostic["message"]
    assert "/private/" not in result["compilation"]["stderr"]["text"]


def test_compiles_and_runs_multiple_translation_units(client: TestClient) -> None:
    require_clang()
    response = post_job(
        client,
        {
            "schema_version": "1.0",
            "request_id": "multi",
            "profile_id": "cpp-clang-c++20-multi",
            "action": "compile_and_run",
            "files": [
                {
                    "path": "include/sum.hpp",
                    "content": "#pragma once\nint sum(int a, int b);\n",
                },
                {
                    "path": "src/sum.cpp",
                    "content": '#include "../include/sum.hpp"\nint sum(int a,int b){return a+b;}\n',
                },
                {
                    "path": "src/main.cpp",
                    "content": '#include "../include/sum.hpp"\n#include <iostream>\nint main(){std::cout<<sum(2,3);}\n',
                },
            ],
            "stdin": "",
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "SUCCESS"
    assert result["execution"]["stdout"]["text"] == "5"
    assert len(result["executable_sha256"]) == 64
    assert result["isolation"]["filesystem_isolated"] is False
    assert result["isolation"]["network"] == "host"
    assert result["isolation"]["policy"] == "UNRESTRICTED_CONTAINER"
    assert "UNRESTRICTED" in result["isolation"]["warning"]


def test_runtime_text_data_is_writable_in_program_working_directory(
    client: TestClient,
) -> None:
    require_clang()
    response = post_job(
        client,
        {
            "schema_version": "1.0",
            "request_id": "runtime-text-data",
            "profile_id": "cpp-clang-c++20-single",
            "action": "compile_and_run",
            "files": [
                {
                    "path": "main.cpp",
                    "content": """
#include <fstream>
#include <filesystem>
#include <iostream>
#include <iterator>
#include <string>
int main(int argc, char **argv) {
  if (argc != 1 || !std::filesystem::exists("program") ||
      !std::filesystem::equivalent(argv[0], "program")) return 5;
  std::ifstream input("fixtures/input.txt");
  if (!input.is_open()) return 2;
  std::string value((std::istreambuf_iterator<char>(input)), {});
  input.close();
  std::ofstream replacement("fixtures/input.txt", std::ios::trunc);
  replacement << "changed";
  replacement.close();
  if (!replacement) return 3;
  const unsigned char bytes[] = {0, 1, 127, 255};
  std::ofstream binary("created.bin", std::ios::binary);
  binary.write(reinterpret_cast<const char *>(bytes), sizeof(bytes));
  binary.close();
  if (!binary) return 4;
  std::cout << value;
}
""",
                },
                {"path": "fixtures/input.txt", "content": "initial data\n"},
            ],
            "stdin": "",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "SUCCESS"
    assert result["execution"]["stdout"]["text"] == "initial data\n"


def test_single_profile_rejects_multiple_sources(client: TestClient) -> None:
    response = post_job(
        client,
        {
            "schema_version": "1.0",
            "request_id": "bad-single",
            "profile_id": "cpp-clang-c++20-single",
            "action": "compile",
            "files": [
                {"path": "one.cpp", "content": "int one(){return 1;}"},
                {"path": "two.cpp", "content": "int two(){return 2;}"},
            ],
            "stdin": "",
        },
    )
    assert response.status_code == 422
    assert "exactly one" in response.text
