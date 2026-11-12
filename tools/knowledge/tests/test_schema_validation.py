# -*- coding: utf-8 -*-
"""最小 schema 校验：required/enum/type/附加字段/文件名与字段路径。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support
from le_knowledge import loader
from le_knowledge.errors import KnowledgeValidationError
from le_knowledge.schema import load_schema_file, validate_document


class SchemaValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = {
            "le-knowledge-mechanism-v1": load_schema_file(_support.SCHEMAS_DIR / "mechanism-v1.json"),
            "le-knowledge-skill-template-v1": load_schema_file(
                _support.SCHEMAS_DIR / "skill-template-v1.json"
            ),
        }

    def test_fixture_documents_pass(self) -> None:
        for document in _support.DEFAULT_MECHANISMS.values():
            errors = validate_document(document, self.schemas[document["schema"]])
            self.assertEqual(errors, [], errors)
        for document in _support.DEFAULT_SKILLS.values():
            errors = validate_document(document, self.schemas[document["schema"]])
            self.assertEqual(errors, [], errors)

    def test_missing_required_field(self) -> None:
        document = _support.mechanism("fixture-missing")
        del document["rule_ref"]
        errors = validate_document(document, self.schemas["le-knowledge-mechanism-v1"])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["pointer"], "$")
        self.assertIn("rule_ref", errors[0]["message"])

    def test_enum_error_points_at_field(self) -> None:
        document = _support.mechanism("fixture-enum", evidence_state="maybe")
        errors = validate_document(document, self.schemas["le-knowledge-mechanism-v1"])
        self.assertEqual([error["pointer"] for error in errors], ["$.evidence_state"])
        self.assertIn("confirmed", errors[0]["message"])

    def test_type_error(self) -> None:
        document = _support.mechanism("fixture-type")
        document["revision"] = "1"
        errors = validate_document(document, self.schemas["le-knowledge-mechanism-v1"])
        self.assertEqual([error["pointer"] for error in errors], ["$.revision"])
        self.assertIn("整数", errors[0]["message"])
        self.assertIn("string", errors[0]["message"])

    def test_additional_properties_rejected(self) -> None:
        document = _support.mechanism("fixture-extra")
        document["unexpected"] = True
        errors = validate_document(document, self.schemas["le-knowledge-mechanism-v1"])
        self.assertEqual([error["pointer"] for error in errors], ["$"])
        self.assertIn("unexpected", errors[0]["message"])

    def test_pattern_error(self) -> None:
        document = _support.mechanism("Bad_ID")
        errors = validate_document(document, self.schemas["le-knowledge-mechanism-v1"])
        self.assertEqual([error["pointer"] for error in errors], ["$.mechanism_id"])

    def test_nested_array_pointer(self) -> None:
        document = _support.mechanism("fixture-nested")
        document["selectors"][0]["kind"] = "unknown-kind"
        errors = validate_document(document, self.schemas["le-knowledge-mechanism-v1"])
        self.assertEqual([error["pointer"] for error in errors], ["$.selectors[0].kind"])

    def test_loader_error_has_file_and_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            knowledge_dir = _support.make_knowledge_dir(temp_root)
            path = knowledge_dir / "mechanisms" / "fixture-ignite.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["evidence_state"] = "unknown-state"
            _support.write_json(path, document)
            with self.assertRaises(KnowledgeValidationError) as caught:
                loader.load_corpus(knowledge_dir, root=temp_root)
            errors = caught.exception.errors
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["path"], "knowledge/mechanisms/fixture-ignite.json")
            self.assertEqual(errors[0]["pointer"], "$.evidence_state")

    def test_loader_reports_broken_json_and_unknown_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            knowledge_dir = _support.make_knowledge_dir(temp_root)
            (knowledge_dir / "mechanisms" / "broken.json").write_text("{not json", encoding="utf-8")
            _support.write_json(
                knowledge_dir / "mechanisms" / "unknown.json",
                {"schema": "le-unknown-v1", "mechanism_id": "x"},
            )
            with self.assertRaises(KnowledgeValidationError) as caught:
                loader.load_corpus(knowledge_dir, root=temp_root)
            pointers = {(error["path"], error["pointer"]) for error in caught.exception.errors}
            self.assertIn(("knowledge/mechanisms/broken.json", "$"), pointers)
            self.assertIn(("knowledge/mechanisms/unknown.json", "$.schema"), pointers)

    def test_loader_reports_schema_directory_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            knowledge_dir = _support.make_knowledge_dir(temp_root)
            skill_document = _support.skill_template("fixture-skill")
            _support.write_json(knowledge_dir / "mechanisms" / "misfiled.json", skill_document)
            with self.assertRaises(KnowledgeValidationError) as caught:
                loader.load_corpus(knowledge_dir, root=temp_root)
            messages = " ".join(error["message"] for error in caught.exception.errors)
            self.assertIn("目录不一致", messages)


if __name__ == "__main__":
    unittest.main()
