"""TR-064 guest WLAN regressions; no router or InfluxDB is required."""

import configparser
import unittest
from unittest.mock import Mock, call, patch

from fritzconnection.core.exceptions import (
    FritzActionError, FritzAuthorizationError, FritzConnectionException,
)
from requests.exceptions import Timeout

from fritzinfluxdb.classes.fritzbox.handler import FritzBoxHandler


def upnp_error(code=504):
    # Same message format as fritzconnection 1.13.2's SOAP error parser.
    return FritzConnectionException(f"UPnPError: \nerrorCode: {code}\nerrorDescription: Action Failed")


class GuestWlanTests(unittest.TestCase):
    info = {
        "NewStatus": "Up",
        "NewChannel": "11",
        "NewSSID": "Guest WLAN",
        "NewStandard": "ax",
    }

    def setUp(self):
        self.config = configparser.ConfigParser()
        self.config.read_dict({"fritzbox": {"username": "test", "password": "test"}})
        self.handler = self.new_handler()
        self.service = self.get_service("WLANConfiguration:3")
        self.action = self.service.actions[0]
        self.primary_result = self.info.copy()
        self.fallback_result = {"NewSSID": "Guest WLAN"}
        self.associations_result = {"NewTotalAssociations": "1"}
        self.handler.session.call_action.side_effect = self.respond
        self.log = patch("fritzinfluxdb.classes.fritzbox.handler.log").start()
        self.addCleanup(patch.stopall)

    def new_handler(self):
        handler = FritzBoxHandler(self.config)
        handler.session = Mock()
        return handler

    def get_service(self, name, handler=None):
        return next(service for service in (handler or self.handler).services if service.name == name)

    def respond(self, service, action, **params):
        result = {
            "GetInfo": self.primary_result,
            "GetSSID": self.fallback_result,
            "GetTotalAssociations": self.associations_result,
        }[action]
        if isinstance(result, Exception):
            raise result
        return result

    def poll(self, service=None):
        service = service or self.service
        # Simulate the next due cycle without waiting for the real interval.
        service.last_query = None
        self.handler.current_result_list = []
        self.handler.query_service_data(service)
        self.handler.discovery_done = True
        return {metric.name: metric.value for metric in self.handler.current_result_list}

    def test_getinfo_keeps_all_existing_metrics(self):
        self.assertEqual(self.poll(), {
            "wlan3_status": "Up", "wlan3_channel": 11,
            "wlan3_ssid": "Guest WLAN", "wlan3_802.11_standard": "ax",
            "wlan3_associations": 1,
        })
        self.handler.session.call_action.assert_has_calls([
            call(self.service.name, "GetInfo"),
            call(self.service.name, "GetTotalAssociations"),
        ])
        self.assertEqual(self.handler.session.call_action.call_count, 2)

    def test_single_504_falls_back_without_disabling(self):
        self.primary_result = upnp_error()
        self.assertEqual(self.poll(), {"wlan3_ssid": "Guest WLAN", "wlan3_associations": 1})
        self.assertEqual(self.action.consecutive_failures, 1)
        self.assertFalse(self.action.primary_disabled)
        self.log.error.assert_not_called()
        self.log.warning.assert_not_called()
        self.handler.session.call_action.assert_has_calls([
            call(self.service.name, "GetInfo"), call(self.service.name, "GetSSID"),
            call(self.service.name, "GetTotalAssociations"),
        ])
        self.poll()
        self.assertEqual(self.action.consecutive_failures, 2)

    def test_three_504s_disable_only_primary_and_warn_once(self):
        self.primary_result = upnp_error()
        for _ in range(3):
            self.poll()
        self.assertTrue(self.action.primary_disabled)
        self.assertTrue(self.action.available)
        self.assertTrue(self.service.available)
        self.log.warning.assert_called_once()
        warning = self.log.warning.call_args[0][0]
        for text in ("WLANConfiguration:3/GetInfo", "504", "GetSSID", "until restart"):
            self.assertIn(text, warning)

        self.handler.session.call_action.reset_mock()
        for _ in range(3):
            self.assertEqual(self.poll(), {"wlan3_ssid": "Guest WLAN", "wlan3_associations": 1})
        self.assertEqual(self.handler.session.call_action.call_args_list, [
            call(self.service.name, "GetSSID"), call(self.service.name, "GetTotalAssociations"),
        ] * 3)
        self.log.warning.assert_called_once()
        self.log.error.assert_not_called()

    def test_success_resets_consecutive_failures_and_restores_metrics(self):
        self.primary_result = upnp_error()
        self.poll()
        self.poll()
        self.primary_result = self.info
        self.assertEqual(len(self.poll()), 5)
        self.assertEqual(self.action.consecutive_failures, 0)
        self.primary_result = upnp_error()
        self.poll()
        self.poll()
        self.assertFalse(self.action.primary_disabled)

    def test_authentication_error_stays_visible_and_breaks_failure_streak(self):
        self.primary_result = upnp_error()
        self.poll()
        self.poll()
        self.primary_result = FritzAuthorizationError("401 Unauthorized")
        self.handler.session.call_action.reset_mock()
        self.assertEqual(self.poll(), {"wlan3_associations": 1})
        self.assertEqual(self.action.consecutive_failures, 0)
        self.assertFalse(self.action.primary_disabled)
        self.log.error.assert_called_once()
        self.assertIn("credentials", self.log.error.call_args[0][0])
        self.assertNotIn(call(self.service.name, "GetSSID"),
                         self.handler.session.call_action.call_args_list)

    def test_timeout_stays_visible_and_breaks_failure_streak(self):
        self.primary_result = upnp_error()
        self.poll()
        self.primary_result = Timeout("connection timed out")
        self.assertEqual(self.poll(), {"wlan3_associations": 1})
        self.assertFalse(self.action.primary_disabled)
        self.assertEqual(self.action.consecutive_failures, 0)
        self.log.error.assert_called_once()

    def test_other_upnp_error_does_not_fall_back(self):
        self.primary_result = upnp_error(501)
        for _ in range(4):
            self.poll()
        self.assertFalse(self.action.primary_disabled)
        self.assertEqual(self.action.consecutive_failures, 0)
        self.assertEqual(self.log.error.call_count, 4)
        self.assertNotIn(call(self.service.name, "GetSSID"),
                         self.handler.session.call_action.call_args_list)

    def test_http_504_is_not_a_upnp_504(self):
        self.primary_result = FritzConnectionException("HTTP 504 Gateway Timeout")
        self.assertEqual(self.poll(), {"wlan3_associations": 1})
        self.assertEqual(self.action.consecutive_failures, 0)
        self.log.error.assert_called_once()

    def test_error_code_attribute_is_preferred(self):
        for attribute in ("error_code", "errorCode"):
            for code in (504, "504"):
                with self.subTest(attribute=attribute, code=code):
                    error = upnp_error(501)
                    setattr(error, attribute, code)
                    self.primary_result = error
                    self.assertEqual(self.poll(), {"wlan3_ssid": "Guest WLAN", "wlan3_associations": 1})
                    self.action.consecutive_failures = 0
        self.log.error.assert_not_called()

    def test_error_code_parser_requires_exact_code(self):
        for message in ("errorCode: 5040", "http://fritz.box:504", "request 504", "errorCode: 504oops"):
            with self.subTest(message=message):
                self.assertNotEqual(FritzBoxHandler._upnp_error_code(FritzConnectionException(message)), 504)

    def test_associations_change_between_zero_and_one_after_disable(self):
        self.primary_result = upnp_error()
        for _ in range(3):
            self.poll()
        for clients in (0, 1, 0, 1):
            self.associations_result = {"NewTotalAssociations": clients}
            self.assertEqual(self.poll()["wlan3_associations"], clients)

    def test_fallback_does_not_reuse_old_getinfo_metrics(self):
        self.assertEqual(len(self.poll()), 5)
        self.primary_result = upnp_error()
        self.assertEqual(self.poll(), {"wlan3_ssid": "Guest WLAN", "wlan3_associations": 1})

    def test_failed_or_missing_fallback_keeps_associations_and_logs_error(self):
        self.primary_result = upnp_error()
        for error in (upnp_error(), FritzActionError("No GetSSID"),
                      FritzAuthorizationError("401 Unauthorized"), Timeout("timed out")):
            with self.subTest(error=type(error).__name__):
                self.fallback_result = error
                self.log.reset_mock()
                self.assertEqual(self.poll(), {"wlan3_associations": 1})
                self.log.error.assert_called_once()
                self.assertIn("GetSSID", self.log.error.call_args[0][0])
                self.assertTrue(self.service.available)
                self.assertTrue(self.action.available)

    def test_fallback_failure_during_discovery_does_not_disable_primary(self):
        self.primary_result = upnp_error()
        self.fallback_result = FritzActionError("No GetSSID")
        self.assertEqual(self.poll(), {"wlan3_associations": 1})
        self.primary_result = self.info
        self.assertEqual(len(self.poll()), 5)

    def test_fallback_failure_does_not_log_soap_or_credentials(self):
        self.primary_result = upnp_error()
        self.fallback_result = FritzConnectionException("errorCode: 501\n<SOAP>password=secret token=secret</SOAP>")
        self.poll()
        message = self.log.error.call_args[0][0]
        self.assertIn("501", message)
        for private in ("SOAP", "password", "token", "secret"):
            self.assertNotIn(private, message)

    def test_associations_failure_is_independent_and_visible(self):
        self.primary_result = upnp_error()
        self.associations_result = upnp_error()
        self.assertEqual(self.poll(), {"wlan3_ssid": "Guest WLAN"})
        self.log.error.assert_called_once()
        self.assertFalse(self.service.actions[1].primary_disabled)

    def test_504_on_other_interfaces_does_not_enable_fallback(self):
        self.primary_result = upnp_error()
        for name in ("WLANConfiguration:1", "WLANConfiguration:2"):
            with self.subTest(name=name):
                service = self.get_service(name)
                self.handler.session.call_action.reset_mock()
                self.log.reset_mock()
                for _ in range(4):
                    self.poll(service)
                self.assertFalse(service.actions[0].primary_disabled)
                self.assertEqual(service.actions[0].consecutive_failures, 0)
                self.assertEqual(self.log.error.call_count, 4)
                self.assertNotIn(call(name, "GetSSID"), self.handler.session.call_action.call_args_list)

    def test_new_handler_retries_primary_after_restart(self):
        self.primary_result = upnp_error()
        for _ in range(3):
            self.poll()
        restarted = self.new_handler()
        service = self.get_service(self.service.name, restarted)
        restarted.session.call_action.side_effect = self.respond
        restarted.query_service_data(service)
        self.assertEqual(restarted.session.call_action.call_args_list[0],
                         call(self.service.name, "GetInfo"))
        self.assertEqual(service.actions[0].consecutive_failures, 1)
        self.assertFalse(service.actions[0].primary_disabled)

    def test_dsl_metrics_are_unchanged_while_guest_fallback_is_active(self):
        self.primary_result = upnp_error()
        for _ in range(3):
            self.poll()
        service = self.get_service("WANDSLInterfaceConfig")
        self.handler.session.call_action.side_effect = None
        self.handler.session.call_action.reset_mock()
        self.handler.session.call_action.side_effect = [
            {"NewDownstreamMaxRate": "120000", "NewUpstreamMaxRate": "45000",
             "NewDownstreamCurrRate": "100000", "NewUpstreamCurrRate": "40000",
             "NewDownstreamNoiseMargin": "60", "NewUpstreamNoiseMargin": "70",
             "NewDownstreamAttenuation": "80", "NewUpstreamAttenuation": "90"},
            {"NewSeverelyErroredSecs": "0", "NewErroredSecs": "2", "NewCRCErrors": "3"},
            {"NewDownstreamPower": "100", "NewUpstreamPower": "110"},
        ]
        self.assertEqual(self.poll(service), {
            "maxBitRate_downstream": 120000, "maxBitRate_upstream": 45000,
            "downstream_dsl_sync_max": 100000, "upstream_dsl_sync_max": 40000,
            "snr_downstream": 60, "snr_upstream": 70,
            "attenuation_downstream": 80, "attenuation_upstream": 90,
            "severely_errored_seconds": 0, "errored_seconds": 2, "crc_errors": 3,
            "power_downstream": 100, "power_upstream": 110,
        })
        self.assertEqual(self.handler.session.call_action.call_args_list, [
            call(service.name, "GetInfo"), call(service.name, "GetStatisticsTotal"),
            call(service.name, "X_AVM-DE_GetDSLInfo"),
        ])
        self.log.error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
