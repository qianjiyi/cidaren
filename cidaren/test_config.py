"""cidaren/config.py 单元测试"""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from cidaren.config import (
    _parse_env_value,
    _format_env_value,
    load_env_file,
    get_runtime_config,
    save_runtime_config,
    get_missing_auth_fields,
    build_subprocess_env,
    ENV_FILE,
    CONFIG_FIELDS,
    DEFAULT_CONFIG,
    REQUIRED_AUTH_FIELDS,
)


# ── _parse_env_value ──


@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param("", "", id="空字符串"),
        pytest.param("   ", "", id="纯空格"),
        pytest.param("hello", "hello", id="普通值"),
        pytest.param("  hello  ", "hello", id="前后空格被strip"),
        pytest.param('"hello world"', "hello world", id="双引号包裹"),
        pytest.param("'hello world'", "hello world", id="单引号包裹"),
        pytest.param('"value with \\"escape\\""', 'value with "escape"', id="双引号包裹含转义"),
        pytest.param("'single'", "single", id="单引号普通值"),
        pytest.param('"a"', "a", id="双引号单字符"),
        pytest.param("'a'", "a", id="单引号单字符"),
        pytest.param("ab", "ab", id="无引号两字符"),
    ],
)
def test_parse_env_value(raw, expected):
    assert _parse_env_value(raw) == expected


def test_parse_env_value_双引号json解析失败时回退():
    # 不合法的JSON双引号 → 回退到去掉首尾引号
    raw = '"bad json"x"'  # 首尾不同 → 不进入引号分支
    # 实际: 首尾都是 " 且长度>=2, 但 JSON 解析会失败 → 回退
    raw2 = '"bad \\ json"'
    result = _parse_env_value(raw2)
    # json.loads 会失败因为 \ 后跟空格不合法 → 回退到 value[1:-1]
    assert result == "bad \\ json"


# ── _format_env_value ──


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param(None, '""', id="None转空引号"),
        pytest.param("", '""', id="空字符串转空引号"),
        pytest.param("simple", "simple", id="简单值不加引号"),
        pytest.param("has space", '"has space"', id="含空格加双引号"),
        pytest.param("has#hash", '"has#hash"', id="含#号加双引号"),
        pytest.param('has"quote', '"has\\"quote"', id="含双引号加转义"),
        pytest.param("has\\back", '"has\\\\back"', id="含反斜杠加转义"),
    ],
)
def test_format_env_value(value, expected):
    assert _format_env_value(value) == expected


# ── load_env_file ──


class TestLoadEnvFile:
    def test_文件不存在时返回空字典(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        assert load_env_file() == {}

    def test_正常解析键值对(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        fake_env.write_text("KEY1=value1\nKEY2=value2\n", encoding="utf-8")
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        result = load_env_file()
        assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_跳过注释和空行(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        fake_env.write_text("# comment\n\nKEY=val\n", encoding="utf-8")
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        result = load_env_file()
        assert result == {"KEY": "val"}

    def test_处理export前缀(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        fake_env.write_text("export MY_KEY=myval\n", encoding="utf-8")
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        result = load_env_file()
        assert result == {"MY_KEY": "myval"}

    def test_值中包含等号(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        fake_env.write_text("URL=https://example.com?a=1&b=2\n", encoding="utf-8")
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        result = load_env_file()
        assert result == {"URL": "https://example.com?a=1&b=2"}

    def test_带引号的值(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        fake_env.write_text('TOKEN="my secret token"\n', encoding="utf-8")
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        result = load_env_file()
        assert result == {"TOKEN": "my secret token"}


# ── get_runtime_config ──


class TestGetRuntimeConfig:
    def test_默认配置(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        # 清除可能干扰的环境变量
        for key in CONFIG_FIELDS:
            monkeypatch.delenv(key, raising=False)
        result = get_runtime_config()
        for key, val in DEFAULT_CONFIG.items():
            assert result[key] == val

    def test_env文件覆盖默认配置(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        fake_env.write_text("USERTOKEN=from_file\n", encoding="utf-8")
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        for key in CONFIG_FIELDS:
            monkeypatch.delenv(key, raising=False)
        result = get_runtime_config()
        assert result["USERTOKEN"] == "from_file"

    def test_环境变量优先级最高(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        fake_env.write_text("USERTOKEN=from_file\n", encoding="utf-8")
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        monkeypatch.setenv("USERTOKEN", "from_env")
        for key in CONFIG_FIELDS:
            if key != "USERTOKEN":
                monkeypatch.delenv(key, raising=False)
        result = get_runtime_config()
        assert result["USERTOKEN"] == "from_env"


# ── save_runtime_config ──


class TestSaveRuntimeConfig:
    def test_保存配置到文件(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        for key in CONFIG_FIELDS:
            monkeypatch.delenv(key, raising=False)

        payload = {"USERTOKEN": "tok123", "ABC": "abc456", "AUTH_V": "v789"}
        result = save_runtime_config(payload)

        assert result["USERTOKEN"] == "tok123"
        assert result["ABC"] == "abc456"
        assert result["AUTH_V"] == "v789"
        # 验证文件已写入
        content = fake_env.read_text(encoding="utf-8")
        assert "tok123" in content
        assert "abc456" in content

    def test_None值转为空字符串(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        for key in CONFIG_FIELDS:
            monkeypatch.delenv(key, raising=False)

        payload = {"USERTOKEN": None}
        result = save_runtime_config(payload)
        assert result["USERTOKEN"] == ""

    def test_保存后环境变量同步更新(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        for key in CONFIG_FIELDS:
            monkeypatch.delenv(key, raising=False)

        payload = {"USERTOKEN": "envtest"}
        save_runtime_config(payload)
        assert os.environ.get("USERTOKEN") == "envtest"

    def test_未在payload中的字段保留默认值(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        for key in CONFIG_FIELDS:
            monkeypatch.delenv(key, raising=False)

        payload = {"USERTOKEN": "tok"}
        result = save_runtime_config(payload)
        # LLM_URL 应该保留默认值
        assert result["LLM_URL"] == DEFAULT_CONFIG["LLM_URL"]


# ── get_missing_auth_fields ──


@pytest.mark.parametrize(
    "config, expected",
    [
        pytest.param(
            {"USERTOKEN": "a", "ABC": "b", "AUTH_V": "c"},
            [],
            id="全部填写",
        ),
        pytest.param(
            {"USERTOKEN": "", "ABC": "b", "AUTH_V": "c"},
            ["USERTOKEN"],
            id="USERTOKEN为空",
        ),
        pytest.param(
            {"USERTOKEN": "  ", "ABC": "", "AUTH_V": ""},
            ["USERTOKEN", "ABC", "AUTH_V"],
            id="全部空白",
        ),
        pytest.param(
            {"USERTOKEN": None, "ABC": None, "AUTH_V": None},
            ["USERTOKEN", "ABC", "AUTH_V"],
            id="所有字段为None",
        ),
    ],
)
def test_get_missing_auth_fields(config, expected):
    assert get_missing_auth_fields(config) == expected


# ── build_subprocess_env ──


class TestBuildSubprocessEnv:
    def test_包含所有CONFIG_FIELDS(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        for key in CONFIG_FIELDS:
            monkeypatch.delenv(key, raising=False)

        config = {"USERTOKEN": "tok", "ABC": "abc", "AUTH_V": "v1"}
        env = build_subprocess_env(config)
        assert env["USERTOKEN"] == "tok"
        assert env["ABC"] == "abc"
        assert env["AUTH_V"] == "v1"
        # 缺失字段用默认值填充
        assert env["LLM_URL"] == DEFAULT_CONFIG["LLM_URL"]

    def test_继承当前环境变量(self, tmp_path, monkeypatch):
        fake_env = tmp_path / ".env"
        monkeypatch.setattr("cidaren.config.ENV_FILE", fake_env)
        monkeypatch.setenv("MY_CUSTOM_VAR", "custom_value")
        for key in CONFIG_FIELDS:
            monkeypatch.delenv(key, raising=False)

        env = build_subprocess_env({"USERTOKEN": "t", "ABC": "a", "AUTH_V": "v"})
        assert env["MY_CUSTOM_VAR"] == "custom_value"
