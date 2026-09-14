import pytest

from stocklab.config.universe import ALLOWED_HOSTS, assert_host_allowed
from stocklab.data.errors import HostNotAllowed


def test_allowed_host_passes():
    assert_host_allowed("https://qt.gtimg.cn/q=sz000333")


def test_numeric_subdomain_rejected():
    with pytest.raises(HostNotAllowed):
        assert_host_allowed("https://1.push2.eastmoney.com/api/qt/clist/get")


def test_ip_literal_rejected():
    with pytest.raises(HostNotAllowed):
        assert_host_allowed("http://192.168.1.10/data")


def test_unknown_host_rejected():
    with pytest.raises(HostNotAllowed):
        assert_host_allowed("https://example.com/x")


def test_whitelist_is_exact():
    assert ALLOWED_HOSTS == frozenset(
        {
            "qt.gtimg.cn",
            "web.ifzq.gtimg.cn",
            "push2.eastmoney.com",
            "push2his.eastmoney.com",
        }
    )
