"""
ConfigService 单元测试
"""
import json
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory

from lark_client.config_service import ConfigService


class TestConfigService:
    """ConfigService 测试类"""

    def test_load_default_config(self):
        """测试加载默认配置"""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            service = ConfigService(config_path)

            # 验证默认值
            assert service.get("version") == "1.0"
            assert service.get("mention.auto_check_enabled") is True
            assert service.get("mention.check_interval_minutes") == 10
            assert service.get("mention.blacklist_chats") == []
            assert service.get("mention.priority_chats") == []

    def test_save_and_load_config(self):
        """测试配置保存和加载"""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"

            # 第一次：保存配置
            service1 = ConfigService(config_path)
            service1.set("mention.check_interval_minutes", 15)
            service1.set("mention.blacklist_chats", ["oc_test1", "oc_test2"])
            service1.save()

            # 第二次：加载配置
            service2 = ConfigService(config_path)
            assert service2.get("mention.check_interval_minutes") == 15
            assert service2.get("mention.blacklist_chats") == ["oc_test1", "oc_test2"]

    def test_validate_interval_range(self):
        """测试 check_interval_minutes 范围验证"""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            service = ConfigService(config_path)

            # 有效值
            service.set("mention.check_interval_minutes", 10)
            service.validate()  # 应该不抛异常

            # 无效值（小于5）
            with pytest.raises(ValueError, match="5-60 范围内"):
                service.set("mention.check_interval_minutes", 3)

            # 无效值（大于60）
            with pytest.raises(ValueError, match="5-60 范围内"):
                service.set("mention.check_interval_minutes", 70)

    def test_validate_interval_type(self):
        """测试 check_interval_minutes 类型验证"""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            service = ConfigService(config_path)

            # 无效类型
            with pytest.raises(TypeError, match="必须是整数类型"):
                service.set("mention.check_interval_minutes", "10")

    def test_validate_auto_check_type(self):
        """测试 auto_check_enabled 类型验证"""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            service = ConfigService(config_path)

            # 有效值
            service.set("mention.auto_check_enabled", False)
            service.validate()  # 应该不抛异常

            # 无效类型
            with pytest.raises(TypeError, match="必须是 bool 类型"):
                service.set("mention.auto_check_enabled", "true")

    def test_validate_chat_id_format(self):
        """测试 chat_id 格式验证"""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            service = ConfigService(config_path)

            # 有效格式（oc_ 前缀）
            service.set("mention.blacklist_chats", ["oc_123456", "ou_789012"])
            service.validate()  # 应该不抛异常

            # 无效格式
            with pytest.raises(ValueError, match="必须以 oc_ 或 ou_ 开头"):
                service.set("mention.blacklist_chats", ["invalid_id"])

    def test_add_to_list(self):
        """测试添加到列表配置"""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            service = ConfigService(config_path)

            # 添加项
            service.add_to_list("mention.blacklist_chats", "oc_test1")
            service.add_to_list("mention.blacklist_chats", "oc_test2")

            blacklist = service.get("mention.blacklist_chats")
            assert len(blacklist) == 2
            assert "oc_test1" in blacklist
            assert "oc_test2" in blacklist

            # 重复添加（应该抛异常）
            with pytest.raises(ValueError, match="已存在"):
                service.add_to_list("mention.blacklist_chats", "oc_test1")

    def test_remove_from_list(self):
        """测试从列表配置移除项"""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            service = ConfigService(config_path)

            # 添加项
            service.set("mention.blacklist_chats", ["oc_test1", "oc_test2"])

            # 移除项
            service.remove_from_list("mention.blacklist_chats", "oc_test1")

            blacklist = service.get("mention.blacklist_chats")
            assert len(blacklist) == 1
            assert "oc_test2" in blacklist

            # 移除不存在的项（应该抛异常）
            with pytest.raises(ValueError, match="不存在"):
                service.remove_from_list("mention.blacklist_chats", "oc_test3")

    def test_get_list(self):
        """测试获取列表配置"""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            service = ConfigService(config_path)

            # 获取空列表
            assert service.get_list("mention.blacklist_chats") == []

            # 设置列表
            service.set("mention.blacklist_chats", ["oc_test1", "oc_test2"])

            # 获取列表
            blacklist = service.get_list("mention.blacklist_chats")
            assert len(blacklist) == 2
            assert "oc_test1" in blacklist

    def test_deep_merge(self):
        """测试深度合并配置"""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"

            # 创建部分配置文件
            partial_config = {
                "mention": {
                    "check_interval_minutes": 20
                }
            }
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(partial_config))

            # 加载配置
            service = ConfigService(config_path)

            # 验证合并结果
            assert service.get("mention.check_interval_minutes") == 20
            assert service.get("mention.auto_check_enabled") is True  # 默认值应该保留
            assert service.get("version") == "1.0"  # 默认值应该保留


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
