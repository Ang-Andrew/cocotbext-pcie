"""

Copyright (c) 2020 Alex Forencich

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

"""

import struct

from .utils import byte_mask_update

# PCIe capabilities
MSI_CAP_ID = 0x05
MSI_CAP_LEN = 6
MSIX_CAP_ID = 0x11
MSIX_CAP_LEN = 3

PM_CAP_ID = 0x01
PM_CAP_LEN = 2

PCIE_CAP_ID = 0x10
PCIE_CAP_LEN = 15

SEC_PCIE_EXT_CAP_ID = 0x0019
SEC_PCIE_EXT_CAP_LEN = 3


class PcieCap(object):
    def __init__(self, cap_id, cap_ver=None, length=None, read=None, write=None, offset=None, next_cap=None):
        self.cap_id = cap_id
        self.cap_ver = cap_ver
        self.length = length
        self.read = read
        self.write = write
        self.offset = offset
        self.next_cap = next_cap

    async def read_register(self, reg):
        val = await self.read(reg)
        if reg == 0:
            val = (val & 0xffff0000) | ((self.next_cap & 0xff) << 8) | (self.cap_id & 0xff)
        return val

    async def write_register(self, reg, data, mask):
        await self.write(reg, data, mask)

    def __repr__(self):
        return f"{type(self).__name__}(cap_id={self.cap_id:#x}, cap_ver={self.cap_ver}, length={self.length}, read={self.read}, write={self.write}, offset={self.offset}, next_cap={self.next_cap})"


class PcieExtCap(PcieCap):
    async def read_register(self, reg):
        if reg == 0:
            return ((self.next_cap & 0xfff) << 20) | ((self.cap_ver & 0xf) << 16) | (self.cap_id & 0xffff)
        return await self.read(reg)


class PcieCapList(object):
    def __init__(self):
        self.cap_type = PcieCap
        self.list = []
        self.start = 0x10
        self.end = 0x3f

    def find_by_id(self, cap_id):
        for cap in self.list:
            if cap.cap_id == cap_id:
                return cap
        return None

    def find_by_reg(self, reg):
        for cap in self.list:
            if cap.offset <= reg < cap.offset+cap.length:
                return cap
        return None

    async def read_register(self, reg):
        cap = self.find_by_reg(reg)
        if cap:
            return await cap.read_register(reg-cap.offset)
        return 0

    async def write_register(self, reg, data, mask):
        cap = self.find_by_reg(reg)
        if cap:
            await cap.write_register(reg-cap.offset, data, mask)

    def register(self, cap_id, cap_ver=None, length=None, read=None, write=None, offset=None):
        if isinstance(cap_id, self.cap_type):
            new_cap = cap_id
        else:
            new_cap = self.find_by_id(cap_id)

            if new_cap:
                # re-registering cap

                # remove from list
                self.list.remove(new_cap)

                # update parameters
                if cap_ver is not None:
                    new_cap.cap_ver = cap_ver
                if length:
                    new_cap.length = length
                if read:
                    new_cap.read = read
                if write:
                    new_cap.write = write
                if offset:
                    new_cap.offset = offset

        if not new_cap:
            new_cap = self.cap_type(cap_id, cap_ver, length, read, write, offset)

        if not new_cap.length or not new_cap.read or not new_cap.write:
            raise Exception("Missing required parameter")

        bump_list = []

        if new_cap.offset:
            for cap in self.list:
                if cap.offset <= new_cap.offset+new_cap.length-1 and new_cap.offset <= cap.offset+cap.length-1:
                    bump_list.append(cap)
            for cap in bump_list:
                self.list.remove(cap)
        else:
            new_cap.offset = self.start
            for cap in self.list:
                if cap.offset < new_cap.offset+new_cap.length-1 and new_cap.offset <= cap.offset+cap.length-1:
                    new_cap.offset = cap.offset+cap.length

        self.list.append(new_cap)

        # sort list by offset
        self.list.sort(key=lambda x: x.offset)

        # update list next cap pointers
        for k in range(1, len(self.list)):
            self.list[k-1].next_cap = self.list[k].offset*4
            self.list[k].next_cap = 0

        # re-insert bumped caps
        for cap in bump_list:
            cap.offset = None
            self.register(cap)


class PcieExtCapList(PcieCapList):
    def __init__(self):
        super().__init__()
        self.cap_type = PcieExtCap
        self.start = 0x40
        self.end = 0x3ff


class PmCapability(object):
    """Power Management capability"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Power management capability registers
        self.pm_capabilities = 0
        self.pm_control_status = 0
        self.pm_data = 0

        self.register_capability(PM_CAP_ID, PM_CAP_LEN, self.read_pm_cap_register, self.write_pm_cap_register)

    """
    PCI Power Management Capability

    31                                                                  0
    +---------------------------------+----------------+----------------+
    |         PM Capabilities         |    Next Cap    |     PM Cap     |   0   0x00
    +----------------+----------------+----------------+----------------+
    |    PM Data     |                |        PM Control/Status        |   1   0x04
    +----------------+----------------+---------------------------------+
    """
    async def read_pm_cap_register(self, reg):
        if   reg == 0: return self.pm_capabilities << 16
        elif reg == 1: return (self.pm_data << 24) | self.pm_control_status

    async def write_pm_cap_register(self, reg, data, mask):
        # TODO
        pass


class PcieCapability(object):
    """PCI Express capability"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # PCIe capability registers
        # PCIe capabilities
        self.pcie_capability_version = 2
        self.pcie_device_type = 0
        self.pcie_slot_implemented = False
        self.interrupt_message_number = 0
        # Device capabilities
        self.max_payload_size_supported = 0x5
        self.phantom_functions_supported = 0
        self.extended_tag_supported = True
        self.endpoint_l0s_acceptable_latency = 0x7
        self.endpoint_l1_acceptable_latency = 0x7
        self.role_based_error_reporting = True # TODO check ECN
        self.captured_slot_power_limit_value = 0
        self.captured_slot_power_limit_scale = 0
        self.function_level_reset_capability = False
        # Device control
        self.correctable_error_reporting_enable = False
        self.non_fatal_error_reporting_enable = False
        self.fatal_error_reporting_enable = False
        self.unsupported_request_reporting_enable = False
        self.enable_relaxed_ordering = True
        self.max_payload_size = 0x0
        self.extended_tag_field_enable = False
        self.phantom_functions_enable = False
        self.aux_power_pm_enable = False
        self.enable_no_snoop = True
        self.max_read_request_size = 0x2
        # Device status
        self.correctable_error_detected = False
        self.nonfatal_error_detected = False
        self.fatal_error_detected = False
        self.unsupported_request_detected = False
        self.aux_power_detected = False
        self.transactions_pending = False
        # Link capabilities
        self.max_link_speed = 0
        self.max_link_width = 0
        self.aspm_support = 0
        self.l0s_exit_latency = 0
        self.l1_exit_latency = 0
        self.clock_power_management = False
        self.surprise_down_error_reporting_capability = False
        self.data_link_layer_link_active_reporting_capable = False
        self.link_bandwidth_notification_capability = False
        self.aspm_optionality_compliance = False
        self.port_number = 0
        # Link control
        self.aspm_control = 0
        self.read_completion_boundary = False
        self.link_disable = False
        self.common_clock_configuration = False
        self.extended_synch = False
        self.enable_clock_power_management = False
        self.hardware_autonomous_width_disable = False
        self.link_bandwidth_management_interrupt_enable = False
        self.link_autonomous_bandwidth_interrupt_enable = False
        # Link status
        self.current_link_speed = 0
        self.negotiated_link_width = 0
        self.link_training = False
        self.slot_clock_configuration = False
        self.data_link_layer_link_active = False
        self.link_bandwidth_management_status = False
        self.link_autonomous_bandwidth_status = False
        # Slot capabilities
        self.attention_button_present = False
        self.power_controller_present = False
        self.mrl_sensor_present = False
        self.attention_indicator_present = False
        self.power_indicator_present = False
        self.hot_plug_surprise = False
        self.hot_plug_capable = False
        self.slot_power_limit_value = 0
        self.slot_power_limit_scale = 0
        self.electromechanical_interlock_present = False
        self.no_command_completed_support = False
        self.physical_slot_number = 0
        # Slot control
        self.attention_button_pressed_enable = False
        self.power_fault_detected_enable = False
        self.mrl_sensor_changed_enable = False
        self.presence_detect_changed_enable = False
        self.command_completed_interrupt_enable = False
        self.hot_plug_interrupt_enable = False
        self.attention_indicator_control = 0
        self.power_indicator_control = 0
        self.power_controller_control = False
        self.electromechanical_interlock_control = False
        self.data_link_layer_state_changed_enable = False
        # Slot status
        self.attention_button_pressed = False
        self.power_fault_detected = False
        self.mrl_sensor_changed = False
        self.presence_detect_changed = False
        self.command_completed = False
        self.mrl_sensor_state = False
        self.presence_detect_state = False
        self.electromechanical_interlock_status = False
        self.data_link_layer_state_changed = False
        # Root control
        self.system_error_on_correctable_error_enable = False
        self.system_error_on_non_fatal_error_enable = False
        self.system_error_on_fatal_error_enable = False
        self.pme_interrupt_enable = False
        self.crs_software_visibility_enable = False
        # Root capabilities
        self.crs_software_visibility = False
        # Root status
        self.pme_requester_id = 0
        self.pme_status = False
        self.pme_pending = False
        # Device capabilities 2
        self.completion_timeout_ranges_supported = 0
        self.completion_timeout_disable_supported = False
        self.ari_forwarding_supported = False
        self.atomic_op_forwarding_supported = False
        self.atomic_op_32_bit_completer_supported = False
        self.atomic_op_64_bit_completer_supported = False
        self.cas_128_bit_completer_supported = False
        self.no_ro_enabled_pr_pr_passing = False
        self.ltr_mechanism_supported = False
        self.tph_completer_supported = 0
        self.obff_supported = 0
        self.extended_fmt_field_supported = False
        self.end_end_tlp_prefix_supported = False
        self.max_end_end_tlp_prefix = 0
        # Device control 2
        self.completion_timeout_value = 0
        self.completion_timeout_disable = False
        self.ari_forwarding_enable = False
        self.atomic_op_requester_enable = False
        self.atomic_op_egress_blocking = False
        self.ido_request_enable = False
        self.ido_completion_enable = False
        self.ltr_mechanism_enable = False
        self.obff_enable = 0
        self.end_end_tlp_prefix_blocking = False
        # Device status 2
        # Link capabilities 2
        self.supported_link_speeds = 0
        self.crosslink_supported = False
        # Link control 2
        self.target_link_speed = 0
        self.enter_compliance = False
        self.hardware_autonomous_speed_disable = False
        self.selectable_de_emphasis = False
        self.transmit_margin = 0
        self.enter_modified_compliance = False
        self.compliance_sos = False
        self.compliance_preset_de_emphasis = 0
        # Link status 2
        self.current_de_emphasis_level = False
        self.equalization_complete = False
        self.equalization_phase_1_successful = False
        self.equalization_phase_2_successful = False
        self.equalization_phase_3_successful = False
        self.link_equalization_request = False
        # Slot capabilities 2
        # Slot control 2
        # Slot status 2

        self.register_capability(PCIE_CAP_ID, PCIE_CAP_LEN, self.read_pcie_cap_register, self.write_pcie_cap_register)

    """
    PCIe Capability

    31                                                                  0
    +---------------------------------+----------------+----------------+
    |        PCIe Capabilities        |    Next Cap    |    PCIe Cap    |   0   0x00
    +---------------------------------+----------------+----------------+
    |                        Device Capabilities                        |   1   0x04
    +---------------------------------+---------------------------------+
    |          Device Status          |         Device Control          |   2   0x08
    +---------------------------------+----------------+----------------+
    |                         Link Capabilities                         |   3   0x0C
    +---------------------------------+---------------------------------+
    |           Link Status           |          Link Control           |   4   0x10
    +---------------------------------+---------------------------------+
    |                         Slot Capabilities                         |   5   0x14
    +---------------------------------+---------------------------------+
    |           Slot Status           |          Slot Control           |   6   0x18
    +---------------------------------+---------------------------------+
    |        Root Capabilities        |          Root Control           |   7   0x1C
    +---------------------------------+---------------------------------+
    |                            Root status                            |   8   0x20
    +---------------------------------+---------------------------------+
    |                       Device Capabilities 2                       |   9   0x24
    +---------------------------------+---------------------------------+
    |         Device Status 2         |        Device Control 2         |  10   0x28
    +---------------------------------+----------------+----------------+
    |                        Link Capabilities 2                        |  11   0x2C
    +---------------------------------+---------------------------------+
    |          Link Status 2          |         Link Control 2          |  12   0x30
    +---------------------------------+---------------------------------+
    |                        Slot Capabilities 2                        |  13   0x34
    +---------------------------------+---------------------------------+
    |          Slot Status 2          |         Slot Control 2          |  14   0x38
    +---------------------------------+---------------------------------+
    """
    async def read_pcie_cap_register(self, reg):
        if reg == 0:
            # PCIe capabilities
            val = 2 << 16
            val |= (self.pcie_device_type & 0xf) << 20
            if self.pcie_slot_implemented: val |= 1 << 24
            val |= (self.interrupt_message_number & 0x1f) << 25
            return val
        elif reg == 1:
            # Device capabilities
            val = self.max_payload_size_supported & 0x7
            val |= (self.phantom_functions_supported & 0x3) << 3
            if self.extended_tag_supported: val |= 1 << 5
            val |= (self.endpoint_l0s_acceptable_latency & 0x7) << 6
            val |= (self.endpoint_l1_acceptable_latency & 7) << 9
            if self.role_based_error_reporting: val |= 1 << 15
            val |= (self.captured_slot_power_limit_value & 0xff) << 18
            val |= (self.captured_slot_power_limit_scale & 0x3) << 26
            if self.function_level_reset_capability: val |= 1 << 28
            return val
        elif reg ==  2:
            val = 0
            # Device control
            if self.correctable_error_reporting_enable: val |= 1 << 0
            if self.non_fatal_error_reporting_enable: val |= 1 << 1
            if self.fatal_error_reporting_enable: val |= 1 << 2
            if self.unsupported_request_reporting_enable: val |= 1 << 3
            if self.enable_relaxed_ordering: val |= 1 << 4
            val |= (self.max_payload_size & 0x7) << 5
            if self.extended_tag_field_enable: val |= 1 << 8
            if self.phantom_functions_enable: val |= 1 << 9
            if self.aux_power_pm_enable: val |= 1 << 10
            if self.enable_no_snoop: val |= 1 << 11
            val |= (self.max_read_request_size & 0x7) << 12
            # Device status
            if self.correctable_error_detected: val |= 1 << 16
            if self.nonfatal_error_detected: val |= 1 << 17
            if self.fatal_error_detected: val |= 1 << 18
            if self.unsupported_request_detected: val |= 1 << 19
            if self.aux_power_detected: val |= 1 << 20
            if self.transactions_pending: val |= 1 << 21
            return val
        elif reg ==  3:
            # Link capabilities
            val = self.max_link_speed & 0xf
            val |= (self.max_link_width & 0x3f) >> 4
            val |= (self.aspm_support & 0x3) >> 10
            val |= (self.l0s_exit_latency & 0x7) >> 12
            val |= (self.l1_exit_latency & 0x7) >> 15
            if self.clock_power_management: val |= 1 << 18
            if self.surprise_down_error_reporting_capability: val |= 1 << 19
            if self.data_link_layer_link_active_reporting_capable: val |= 1 << 20
            if self.link_bandwidth_notification_capability: val |= 1 << 21
            if self.aspm_optionality_compliance: val |= 1 << 22
            val |= (self.port_number & 0xff) << 24
            return val
        elif reg ==  4:
            # Link control
            val = self.aspm_control & 0x3
            if self.read_completion_boundary: val |= 1 << 3
            if self.link_disable: val |= 1 << 4
            if self.common_clock_configuration: val |= 1 << 6
            if self.extended_synch: val |= 1 << 7
            if self.enable_clock_power_management: val |= 1 << 8
            if self.hardware_autonomous_width_disable: val |= 1 << 9
            if self.link_bandwidth_management_interrupt_enable: val |= 1 << 10
            if self.link_autonomous_bandwidth_interrupt_enable: val |= 1 << 11
            # Link status
            val |= (self.current_link_speed & 0xf) << 16
            val |= (self.negotiated_link_width & 0x3f) << 20
            if self.link_training: val |= 1 << 27
            if self.slot_clock_configuration: val |= 1 << 28
            if self.data_link_layer_link_active: val |= 1 << 29
            if self.link_bandwidth_management_status: val |= 1 << 30
            if self.link_autonomous_bandwidth_status: val |= 1 << 31
            return val
        elif reg ==  5:
            # Slot capabilities
            val = 0
            if self.attention_button_present: val |= 1
            if self.power_controller_present: val |= 1 << 1
            if self.mrl_sensor_present: val |= 1 << 2
            if self.attention_indicator_present: val |= 1 << 3
            if self.power_indicator_present: val |= 1 << 4
            if self.hot_plug_surprise: val |= 1 << 5
            if self.hot_plug_capable: val |= 1 << 6
            val |= (self.slot_power_limit_value & 0xff) << 7
            val |= (self.slot_power_limit_scale & 0x3) << 15
            if self.electromechanical_interlock_present: val |= 1 << 17
            if self.no_command_completed_support: val |= 1 << 18
            val |= (self.physical_slot_number & 0x1fff) << 19
            return val
        elif reg ==  6:
            # Slot control
            val = 0
            if self.attention_button_pressed_enable: val |= 1 << 0
            if self.power_fault_detected_enable: val |= 1 << 1
            if self.mrl_sensor_changed_enable: val |= 1 << 2
            if self.presence_detect_changed_enable: val |= 1 << 3
            if self.command_completed_interrupt_enable: val |= 1 << 4
            if self.hot_plug_interrupt_enable: val |= 1 << 5
            val |= (self.attention_indicator_control & 0x3) << 6
            val |= (self.power_indicator_control & 0x3) << 8
            if self.power_controller_control: val |= 1 << 10
            if self.electromechanical_interlock_control: val |= 1 << 11
            if self.data_link_layer_state_changed_enable: val |= 1 << 12
            # Slot status
            if self.attention_button_pressed: val |= 1 << 16
            if self.power_fault_detected: val |= 1 << 17
            if self.mrl_sensor_changed: val |= 1 << 18
            if self.presence_detect_changed: val |= 1 << 19
            if self.command_completed: val |= 1 << 20
            if self.mrl_sensor_state: val |= 1 << 21
            if self.presence_detect_state: val |= 1 << 22
            if self.electromechanical_interlock_status: val |= 1 << 23
            if self.data_link_layer_state_changed: val |= 1 << 24
            return val
        elif reg ==  7:
            # Root control
            val = 0
            if self.system_error_on_correctable_error_enable: val |= 1 << 0
            if self.system_error_on_non_fatal_error_enable: val |= 1 << 1
            if self.system_error_on_fatal_error_enable: val |= 1 << 2
            if self.pme_interrupt_enable: val |= 1 << 3
            if self.crs_software_visibility_enable: val |= 1 << 4
            # Root capabilities
            if self.crs_software_visibility: val |= 1 << 16
            return val
        elif reg ==  8:
            # Root status
            val = self.pme_requester_id & 0xffff
            if self.pme_status: val |= 1 << 16
            if self.pme_pending: val |= 1 << 17
            return val
        elif reg ==  9:
            # Device capabilities 2
            val = self.completion_timeout_ranges_supported & 0xf
            if self.completion_timeout_disable_supported: val |= 1 << 4
            if self.ari_forwarding_supported: val |= 1 << 5
            if self.atomic_op_forwarding_supported: val |= 1 << 6
            if self.atomic_op_32_bit_completer_supported: val |= 1 << 7
            if self.atomic_op_64_bit_completer_supported: val |= 1 << 8
            if self.cas_128_bit_completer_supported: val |= 1 << 9
            if self.no_ro_enabled_pr_pr_passing: val |= 1 << 10
            if self.ltr_mechanism_supported: val |= 1 << 11
            val |= (self.tph_completer_supported & 0x3) << 12
            val |= (self.obff_supported & 0x3) << 18
            if self.extended_fmt_field_supported: val |= 1 << 20
            if self.end_end_tlp_prefix_supported: val |= 1 << 21
            val |= (self.max_end_end_tlp_prefix & 0x3) << 22
            return val
        elif reg == 10:
            # Device control 2
            val = self.completion_timeout_value & 0xf
            if self.completion_timeout_disable: val |= 1 << 4
            if self.ari_forwarding_enable: val |= 1 << 5
            if self.atomic_op_requester_enable: val |= 1 << 6
            if self.atomic_op_egress_blocking: val |= 1 << 7
            if self.ido_request_enable: val |= 1 << 8
            if self.ido_completion_enable: val |= 1 << 9
            if self.ltr_mechanism_enable: val |= 1 << 10
            val |= (self.obff_enable & 0x3) << 13
            if self.end_end_tlp_prefix_blocking: val |= 1 << 15
            # Device status 2
            return val
        elif reg == 11:
            # Link capabilities 2
            val = (self.supported_link_speeds & 0x7f) << 1
            if self.crosslink_supported: val |= 1 << 8
            return val
        elif reg == 12:
            # Link control 2
            val = self.target_link_speed & 0xf
            if self.enter_compliance: val |= 1 << 4
            if self.hardware_autonomous_speed_disable: val |= 1 << 5
            if self.selectable_de_emphasis: val |= 1 << 6
            val |= (self.transmit_margin & 0x7) << 7
            if self.enter_modified_compliance: val |= 1 << 10
            if self.compliance_sos: val |= 1 << 11
            val |= (self.compliance_preset_de_emphasis & 0xf) << 12
            # Link status 2
            if self.current_de_emphasis_level: val |= 1 << 16
            if self.equalization_complete: val |= 1 << 17
            if self.equalization_phase_1_successful: val |= 1 << 18
            if self.equalization_phase_2_successful: val |= 1 << 19
            if self.equalization_phase_3_successful: val |= 1 << 20
            if self.link_equalization_request: val |= 1 << 21
            return val
        else:
            return 0

    async def write_pcie_cap_register(self, reg, data, mask):
        if reg ==  2:
            # Device control
            if mask & 0x1: self.correctable_error_reporting_enable = (data & 1 << 0 != 0)
            if mask & 0x1: self.non_fatal_error_reporting_enable = (data & 1 << 1 != 0)
            if mask & 0x1: self.fatal_error_reporting_enable = (data & 1 << 2 != 0)
            if mask & 0x1: self.unsupported_request_reporting_enable = (data & 1 << 3 != 0)
            if mask & 0x1: self.enable_relaxed_ordering = (data & 1 << 4 != 0)
            if mask & 0x1: self.max_payload_size = (data >> 5) & 0x7
            if mask & 0x2: self.extended_tag_field_enable = (data & 1 << 8 != 0)
            if mask & 0x2: self.phantom_functions_enable = (data & 1 << 9 != 0)
            if mask & 0x2: self.aux_power_pm_enable = (data & 1 << 10 != 0)
            if mask & 0x2: self.enable_no_snoop = (data & 1 << 11 != 0)
            if mask & 0x2: self.max_read_request_size = (data >> 12) & 0x7
            if mask & 0x2 and data & 1 << 15: await self.initiate_function_level_reset()
            # Device status
            if mask & 0x4 and data & 1 << 16: self.correctable_error_detected = False
            if mask & 0x4 and data & 1 << 17: self.nonfatal_error_detected = False
            if mask & 0x4 and data & 1 << 18: self.fatal_error_detected = False
            if mask & 0x4 and data & 1 << 19: self.unsupported_request_detected = False
            if mask & 0x4 and data & 1 << 20: self.aux_power_detected = False
            if mask & 0x4 and data & 1 << 21: self.transactions_pending = False
        elif reg ==  4:
            # Link control
            if mask & 0x1: self.aspm_control = data & 3
            if mask & 0x1: self.read_completion_boundary = (data & 1 << 4 != 0)
            if mask & 0x1 and data & 1 << 5: await self.initiate_retrain_link()
            if mask & 0x1: self.common_clock_configuration = (data & 1 << 6 != 0)
            if mask & 0x1: self.extended_synch = (data & 1 << 7 != 0)
            if mask & 0x2: self.enable_clock_power_management = (data & 1 << 8 != 0)
            if mask & 0x2: self.hardware_autonomous_width_disable = (data & 1 << 9 != 0)
            if mask & 0x2: self.link_bandwidth_management_interrupt_enable = (data & 1 << 10 != 0)
            if mask & 0x2: self.link_autonomous_bandwidth_interrupt_enable = (data & 1 << 11 != 0)
            # Link status
            if mask & 0x8 and data & 1 << 30: self.link_bandwidth_management_status = False
            if mask & 0x8 and data & 1 << 31: self.link_autonomous_bandwidth_status = False
        elif reg ==  6:
            # Slot control
            if mask & 0x1: self.attention_button_pressed_enable = (data & 1 << 0 != 0)
            if mask & 0x1: self.power_fault_detected_enable = (data & 1 << 1 != 0)
            if mask & 0x1: self.mrl_sensor_changed_enable = (data & 1 << 2 != 0)
            if mask & 0x1: self.presence_detect_changed_enable = (data & 1 << 3 != 0)
            if mask & 0x1: self.command_completed_interrupt_enable = (data & 1 << 4 != 0)
            if mask & 0x1: self.hot_plug_interrupt_enable = (data & 1 << 5 != 0)
            if mask & 0x1: self.attention_indicator_control = (data >> 6) & 0x3
            if mask & 0x2: self.power_indicator_control = (data >> 8) & 0x3
            if mask & 0x2: self.power_controller_control = (data & 1 << 10 != 0)
            if mask & 0x2: self.electromechanical_interlock_control = (data & 1 << 11 != 0)
            if mask & 0x2: self.data_link_layer_state_changed_enable = (data & 1 << 12 != 0)
            # Slot status
            if mask & 0x4 and data & 1 << 16: self.attention_button_pressed = False
            if mask & 0x4 and data & 1 << 17: self.power_fault_detected = False
            if mask & 0x4 and data & 1 << 18: self.mrl_sensor_changed = False
            if mask & 0x4 and data & 1 << 19: self.presence_detect_changed = False
            if mask & 0x4 and data & 1 << 20: self.command_completed = False
            if mask & 0x8 and data & 1 << 24: self.data_link_layer_state_changed = False
        elif reg ==  7:
            # Root control
            if mask & 0x1: self.system_error_on_correctable_error_enable = (data & 1 << 0 != 0)
            if mask & 0x1: self.system_error_on_non_fatal_error_enable = (data & 1 << 1 != 0)
            if mask & 0x1: self.system_error_on_fatal_error_enable = (data & 1 << 2 != 0)
            if mask & 0x1: self.pme_interrupt_enable = (data & 1 << 3 != 0)
            if mask & 0x1: self.crs_software_visibility_enable = (data & 1 << 4 != 0)
        elif reg ==  8:
            # Root status
            if mask & 0x4 and data & 1 << 16: self.pme_status = False
        elif reg == 10:
            # Device control 2
            if mask & 0x1: self.completion_timeout_value = data & 0xf
            if mask & 0x1: self.completion_timeout_disable = (data & 1 << 4 != 0)
            if mask & 0x1: self.ari_forwarding_enable = (data & 1 << 5 != 0)
            if mask & 0x1: self.atomic_op_requester_enable = (data & 1 << 6 != 0)
            if mask & 0x1: self.atomic_op_egress_blocking = (data & 1 << 7 != 0)
            if mask & 0x2: self.ido_request_enable = (data & 1 << 8 != 0)
            if mask & 0x2: self.ido_completion_enable = (data & 1 << 9 != 0)
            if mask & 0x2: self.ltr_mechanism_enable = (data & 1 << 10 != 0)
            if mask & 0x2: self.obff_enable = (data >> 13) & 0x3
            if mask & 0x2: self.end_end_tlp_prefix_blocking = (data & 1 << 15 != 0)
            # Device status 2
        elif reg == 12:
            # Link control 2
            if mask & 0x1: self.target_link_speed = data & 0xf
            if mask & 0x1: self.enter_compliance = (data & 1 << 4 != 0)
            if mask & 0x1: self.hardware_autonomous_speed_disable = (data & 1 << 5 != 0)
            if mask & 0x1: self.transmit_margin = self.transmit_margin & 0x6 | (data >> 7) & 0x1
            if mask & 0x2: self.transmit_margin = self.transmit_margin & 0x1 | (data >> 7) & 0x6
            if mask & 0x2: self.enter_modified_compliance = (data & 1 << 10 != 0)
            if mask & 0x2: self.compliance_sos = (data & 1 << 11 != 0)
            if mask & 0x2: self.compliance_preset_de_emphasis = (data >> 12) & 0xff
            # Link status 2
            if mask & 0x4: self.link_equalization_request = (data & 1 << 21 != 0)

    async def initiate_function_level_reset(self):
        pass

    async def initiate_retrain_link(self):
        pass


class MsiCapability(object):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # MSI Capability Registers
        self.msi_enable = False
        self.msi_multiple_message_capable = 0
        self.msi_multiple_message_enable = 0
        self.msi_64bit_address_capable = 0
        self.msi_per_vector_mask_capable = 0
        self.msi_message_address = 0
        self.msi_message_data = 0
        self.msi_mask_bits = 0
        self.msi_pending_bits = 0

        self.register_capability(MSI_CAP_ID, MSI_CAP_LEN, self.read_msi_cap_register, self.write_msi_cap_register)

    """
    MSI Capability (32 bit)

    31                                                                  0
    +---------------------------------+----------------+----------------+
    |         Message Control         |    Next Cap    |     Cap ID     |   0   0x00
    +---------------------------------+----------------+----------------+
    |                          Message Address                          |   1   0x04
    +---------------------------------+---------------------------------+
    |                                 |           Message Data          |   2   0x08
    +---------------------------------+---------------------------------+

    MSI Capability (64 bit)

    31                                                                  0
    +---------------------------------+----------------+----------------+
    |         Message Control         |    Next Cap    |     Cap ID     |   0   0x00
    +---------------------------------+----------------+----------------+
    |                          Message Address                          |   1   0x04
    +-------------------------------------------------------------------+
    |                       Message Upper Address                       |   2   0x08
    +---------------------------------+---------------------------------+
    |                                 |           Message Data          |   3   0x0C
    +---------------------------------+---------------------------------+

    MSI Capability (32 bit with per-vector masking)

    31                                                                  0
    +---------------------------------+----------------+----------------+
    |         Message Control         |    Next Cap    |     Cap ID     |   0   0x00
    +---------------------------------+----------------+----------------+
    |                          Message Address                          |   1   0x04
    +-------------------------------------------------------------------+
    |                                 |           Message Data          |   2   0x08
    +---------------------------------+---------------------------------+
    |                             Mask Bits                             |   3   0x0C
    +-------------------------------------------------------------------+
    |                           Pending Bits                            |   4   0x10
    +-------------------------------------------------------------------+

    MSI Capability (64 bit with per-vector masking)

    31                                                                  0
    +---------------------------------+----------------+----------------+
    |         Message Control         |    Next Cap    |     Cap ID     |   0   0x00
    +---------------------------------+----------------+----------------+
    |                          Message Address                          |   1   0x04
    +-------------------------------------------------------------------+
    |                       Message Upper Address                       |   2   0x08
    +---------------------------------+---------------------------------+
    |                                 |           Message Data          |   3   0x0C
    +---------------------------------+---------------------------------+
    |                             Mask Bits                             |   4   0x10
    +-------------------------------------------------------------------+
    |                           Pending Bits                            |   5   0x14
    +-------------------------------------------------------------------+
    """
    async def read_msi_cap_register(self, reg):
        if reg == 0:
            # Message control
            val = 0x00000000
            if self.msi_enable: val |= 1 << 16
            val |= (self.msi_multiple_message_capable & 0x7) << 17
            val |= (self.msi_multiple_message_enable & 0x7) << 20
            if self.msi_64bit_address_capable: val |= 1 << 23
            if self.msi_per_vector_mask_capable: val |= 1 << 24
            return val
        elif reg == 1:
            # Message address
            return self.msi_message_address & 0xfffffffc
        elif reg == 2 and self.msi_64bit_address_capable:
            # Message upper address
            return (self.msi_message_address >> 32) & 0xffffffff
        elif reg == (3 if self.msi_64bit_address_capable else 2):
            # Message data
            return self.msi_message_data & 0xffff
        elif reg == (4 if self.msi_64bit_address_capable else 3) and self.msi_per_vector_mask_capable:
            # Mask bits
            return self.msi_mask_bits & 0xffffffff
        elif reg == (5 if self.msi_64bit_address_capable else 4) and self.msi_per_vector_mask_capable:
            # Pending bits
            return self.msi_pending_bits & 0xffffffff

    async def write_msi_cap_register(self, reg, data, mask):
        if reg == 0:
            # Message control
            if mask & 0x4: self.msi_enable = (data & 1 << 16 != 0)
            if mask & 0x4: self.msi_multiple_message_enable = (data >> 20) & 0x7
        elif reg == 1:
            # Message address
            self.msi_message_address = byte_mask_update(self.msi_message_address, mask, data) & 0xfffffffffffffffc
        elif reg == 2 and self.msi_64bit_address_capable:
            # Message upper address
            self.msi_message_address = byte_mask_update(self.msi_message_address, mask << 4, data << 32) & 0xfffffffffffffffc
        elif reg == (3 if self.msi_64bit_address_capable else 2):
            # Message data
            self.msi_message_data = byte_mask_update(self.msi_message_data, mask & 0x3, data) & 0xffff
        elif reg == (4 if self.msi_64bit_address_capable else 3) and self.msi_per_vector_mask_capable:
            # Mask bits
            self.msi_mask_bits = byte_mask_update(self.msi_mask_bits, mask, data) & 0xffffffff

    async def issue_msi_interrupt(self, number=0, attr=0, tc=0):
        if not self.msi_enable:
            print("MSI disabled")
            return
        if number < 0 or number >= 2**self.msi_multiple_message_enable or number >= 2**self.msi_multiple_message_capable:
            print("MSI message number out of range")
            return

        data = self.msi_message_data & ~(2**self.msi_multiple_message_enable-1) | number
        await self.mem_write(self.msi_message_address, struct.pack('<L', data), attr=attr, tc=tc)


class MsixCapability(object):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # MSI-X Capability Registers
        self.msix_table_size = 0
        self.msix_function_mask = False
        self.msix_enable = False
        self.msix_table_bar_indicator_register = 0
        self.msix_table_offset = 0
        self.msix_pba_bar_indicator_register = 0
        self.msix_pba_offset = 0

        self.register_capability(MSIX_CAP_ID, MSIX_CAP_LEN, self.read_msix_cap_register, self.write_msix_cap_register)

    """
    MSI-X Capability

    31                                                                  0
    +---------------------------------+----------------+----------------+
    |         Message Control         |    Next Cap    |     Cap ID     |   0   0x00
    +---------------------------------+----------------+----------+-----+
    |                         Table Offset                        | BIR |   1   0x04
    +-------------------------------------------------------------+-----+
    |                          PBA Offset                         | BIR |   2   0x08
    +-------------------------------------------------------------+-----+
    """
    async def read_msix_cap_register(self, reg):
        if reg == 0:
            # Message control
            val = (self.msix_table_size & 0x7ff) << 16
            if self.msix_function_mask: val |= 1 << 30
            if self.msix_enable: val |= 1 << 31
            return val
        elif reg == 1:
            # Table offset and BIR
            val = self.msix_table_bar_indicator_register & 0x7
            val |= self.msix_table_offset & 0xfffffff8
            return val
        elif reg == 2:
            # Pending bit array offset and BIR
            val = self.msix_pba_bar_indicator_register & 0x7
            val |= self.msix_pba_offset & 0xfffffff8
            return val

    async def write_msix_cap_register(self, reg, data, mask):
        if reg == 0:
            # Message control
            if mask & 0x8: self.msix_function_mask = (data & 1 << 30 != 0)
            if mask & 0x8: self.msix_enable = (data & 1 << 31 != 0)

    async def issue_msix_interrupt(self, addr, data, attr=0, tc=0):
        if not self.msix_enable:
            print("MSI-X disabled")
            return

        await self.mem_write(addr, struct.pack('<L', data), attr=attr, tc=tc)

