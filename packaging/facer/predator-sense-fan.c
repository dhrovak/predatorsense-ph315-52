
/*
 * PredatorSense fan control and sensors, appended to facer.c by the PredatorSense installer.
 * Copyright (C) 2026 Dhruv Bhat. Licensed under the GNU GPL, version 3 or later.
 * Write "<cpu> <gpu>" to /sys/module/facer/parameters/fan_control, where each
 * value is "auto", "max" or a speed in percent (0-100).
 * /sys/module/facer/parameters/sensors (root only) reports the firmware's own
 * temperature and fan speed readings, "-" when unavailable.
 * Write 1 or 0 to /sys/module/facer/parameters/kb_timeout to turn the keyboard
 * backlight's 30 second idle timeout on or off.
 */
MODULE_VERSION("#VERSION#");

#define PS_SYS_INFO_METHODID		5
#define PS_SENSOR_CMD_SUPPORTED		0x0000
#define PS_SENSOR_CMD_READ		0x0001
#define PS_FAN_BEHAVIOR_METHODID	14
#define PS_FAN_SPEED_METHODID		16
#define PS_FAN_BEHAVIOR_CPU		BIT(0)
#define PS_FAN_BEHAVIOR_GPU		BIT(3)
#define PS_FAN_CPU_MODE_SHIFT		16
#define PS_FAN_GPU_MODE_SHIFT		22
#define PS_FAN_SPEED_ID_CPU		0x01
#define PS_FAN_SPEED_ID_GPU		0x04
#define PS_FAN_MODE_AUTO		1
#define PS_FAN_MODE_MAX			2
#define PS_FAN_MODE_CUSTOM		3

static DEFINE_MUTEX(ps_fan_lock);
static char ps_fan_state[32] = "auto auto";

static int ps_fan_call(u32 method_id, u64 in)
{
	u64 out = 0;
	acpi_status status = WMI_gaming_execute_u64(method_id, in, &out);

	if (ACPI_FAILURE(status)) {
		pr_err("fan_control: WMI method %u(0x%llx) failed: %s\n",
		       method_id, in, acpi_format_exception(status));
		return -EIO;
	}
	/* Low byte of the result is the firmware status; non-zero means rejected. */
	if (out & 0xFF) {
		pr_err("fan_control: WMI method %u(0x%llx) returned 0x%llx\n", method_id, in, out);
		return -EIO;
	}
	return 0;
}

static int ps_fan_set_modes(int cpu_mode, int gpu_mode)
{
	u64 in = PS_FAN_BEHAVIOR_CPU | PS_FAN_BEHAVIOR_GPU |
		 ((u64)cpu_mode << PS_FAN_CPU_MODE_SHIFT) |
		 ((u64)gpu_mode << PS_FAN_GPU_MODE_SHIFT);

	return ps_fan_call(PS_FAN_BEHAVIOR_METHODID, in);
}

static int ps_fan_parse(const char *tok, int *mode, unsigned int *percent)
{
	if (!strcmp(tok, "auto")) {
		*mode = PS_FAN_MODE_AUTO;
		return 0;
	}
	if (!strcmp(tok, "max")) {
		*mode = PS_FAN_MODE_MAX;
		return 0;
	}
	if (kstrtouint(tok, 10, percent) || *percent > 100)
		return -EINVAL;
	*mode = PS_FAN_MODE_CUSTOM;
	return 0;
}

static void ps_fan_format(char *buf, size_t len, int mode, unsigned int percent)
{
	if (mode == PS_FAN_MODE_AUTO)
		strscpy(buf, "auto", len);
	else if (mode == PS_FAN_MODE_MAX)
		strscpy(buf, "max", len);
	else
		snprintf(buf, len, "%u", percent);
}

static int ps_fan_control_set(const char *val, const struct kernel_param *kp)
{
	char cpu_tok[8], gpu_tok[8];
	int cpu_mode, gpu_mode, err;
	unsigned int cpu_pct = 0, gpu_pct = 0;

	if (sscanf(val, "%7s %7s", cpu_tok, gpu_tok) != 2 ||
	    ps_fan_parse(cpu_tok, &cpu_mode, &cpu_pct) ||
	    ps_fan_parse(gpu_tok, &gpu_mode, &gpu_pct))
		return -EINVAL;
	if (!wmi_has_guid(WMID_GUID4))
		return -ENODEV;

	mutex_lock(&ps_fan_lock);
	err = ps_fan_set_modes(cpu_mode, gpu_mode);
	if (!err && cpu_mode == PS_FAN_MODE_CUSTOM)
		err = ps_fan_call(PS_FAN_SPEED_METHODID, ((u64)cpu_pct << 8) | PS_FAN_SPEED_ID_CPU);
	if (!err && gpu_mode == PS_FAN_MODE_CUSTOM)
		err = ps_fan_call(PS_FAN_SPEED_METHODID, ((u64)gpu_pct << 8) | PS_FAN_SPEED_ID_GPU);
	if (err) {
		ps_fan_set_modes(PS_FAN_MODE_AUTO, PS_FAN_MODE_AUTO);
		strscpy(ps_fan_state, "auto auto", sizeof(ps_fan_state));
		turbo_state = 0;
	} else {
		ps_fan_format(cpu_tok, sizeof(cpu_tok), cpu_mode, cpu_pct);
		ps_fan_format(gpu_tok, sizeof(gpu_tok), gpu_mode, gpu_pct);
		snprintf(ps_fan_state, sizeof(ps_fan_state), "%s %s", cpu_tok, gpu_tok);
		/* Keep facer's Turbo key toggle in step with what was set here. */
		turbo_state = cpu_mode == PS_FAN_MODE_MAX && gpu_mode == PS_FAN_MODE_MAX;
	}
	mutex_unlock(&ps_fan_lock);
	return err;
}

/* Called by facer's Turbo key handler after it switched both fans itself. */
static void ps_fan_turbo_changed(int on)
{
	mutex_lock(&ps_fan_lock);
	strscpy(ps_fan_state, on ? "max max" : "auto auto", sizeof(ps_fan_state));
	mutex_unlock(&ps_fan_lock);
}

static int ps_fan_control_get(char *buffer, const struct kernel_param *kp)
{
	int len;

	mutex_lock(&ps_fan_lock);
	len = scnprintf(buffer, PAGE_SIZE, "%s\n", ps_fan_state);
	mutex_unlock(&ps_fan_lock);
	return len;
}

static const struct kernel_param_ops ps_fan_control_ops = {
	.set = ps_fan_control_set,
	.get = ps_fan_control_get,
};
module_param_cb(fan_control, &ps_fan_control_ops, NULL, 0644);
MODULE_PARM_DESC(fan_control, "PredatorSense fan control: \"<cpu> <gpu>\", each auto, max or 0-100");

static DEFINE_MUTEX(ps_sensor_lock);
static u64 ps_sensor_supported;
static bool ps_sensor_supported_known;

static int ps_sys_info(u64 in, u64 *out)
{
	acpi_status status = WMI_gaming_execute_u64(PS_SYS_INFO_METHODID, in, out);

	if (ACPI_FAILURE(status) || (*out & 0xFF))
		return -EIO;
	return 0;
}

static int ps_sensors_get(char *buffer, const struct kernel_param *kp)
{
	/* Sensor ids from the Predator Sense v4 interface (see mainline acer-wmi). */
	static const struct {
		const char *name;
		u8 id;
	} sensors[] = {
		{ "cpu_temp", 0x01 },
		{ "gpu_temp", 0x0A },
		{ "cpu_rpm", 0x02 },
		{ "gpu_rpm", 0x06 },
	};
	u64 out = 0;
	int i, len;

	if (!wmi_has_guid(WMID_GUID4))
		return -ENODEV;

	mutex_lock(&ps_sensor_lock);
	if (!ps_sensor_supported_known && !ps_sys_info(PS_SENSOR_CMD_SUPPORTED, &out)) {
		ps_sensor_supported = (out >> 24) & 0xFFFF;
		ps_sensor_supported_known = true;
	}
	len = scnprintf(buffer, PAGE_SIZE, "supported=0x%04llx", ps_sensor_supported);
	for (i = 0; i < ARRAY_SIZE(sensors); i++) {
		u64 in = ((u64)sensors[i].id << 8) | PS_SENSOR_CMD_READ;

		if ((ps_sensor_supported & BIT_ULL(sensors[i].id - 1)) && !ps_sys_info(in, &out))
			len += scnprintf(buffer + len, PAGE_SIZE - len, " %s=%llu",
					 sensors[i].name, (out >> 8) & 0xFFFF);
		else
			len += scnprintf(buffer + len, PAGE_SIZE - len, " %s=-", sensors[i].name);
	}
	mutex_unlock(&ps_sensor_lock);
	len += scnprintf(buffer + len, PAGE_SIZE - len, "\n");
	return len;
}

static int ps_sensors_set(const char *val, const struct kernel_param *kp)
{
	return -EPERM;
}

static const struct kernel_param_ops ps_sensors_ops = {
	.set = ps_sensors_set,
	.get = ps_sensors_get,
};
module_param_cb(sensors, &ps_sensors_ops, NULL, 0400);
MODULE_PARM_DESC(sensors, "PredatorSense firmware sensor readings (read only)");

/*
 * Keyboard backlight 30 second idle timeout, through the WMID_GUID3 "set device
 * status" call PredatorSense uses (hotkey 0x84; byte 5 is the timeout in seconds).
 * The firmware's read-back is unreliable on the PH315-52, so the last value set is reported.
 */
#define PS_SET_DEVICE_STATUS_METHODID	1
#define PS_KB_TIMEOUT_OFF		0x0000000000088402ULL
#define PS_KB_TIMEOUT_30S		0x00001E0000088402ULL

static DEFINE_MUTEX(ps_kb_timeout_lock);
static int ps_kb_timeout_state = -1;

static int ps_kb_timeout_set(const char *val, const struct kernel_param *kp)
{
	struct acpi_buffer output = { ACPI_ALLOCATE_BUFFER, NULL };
	struct acpi_buffer input;
	acpi_status status;
	bool on;
	u64 in;

	if (kstrtobool(val, &on))
		return -EINVAL;
	if (!wmi_has_guid(WMID_GUID3))
		return -ENODEV;

	in = on ? PS_KB_TIMEOUT_30S : PS_KB_TIMEOUT_OFF;
	input.length = sizeof(in);
	input.pointer = &in;

	mutex_lock(&ps_kb_timeout_lock);
	status = wmi_evaluate_method(WMID_GUID3, 0, PS_SET_DEVICE_STATUS_METHODID, &input, &output);
	kfree(output.pointer);
	if (ACPI_FAILURE(status)) {
		mutex_unlock(&ps_kb_timeout_lock);
		pr_err("kb_timeout: WMI call failed: %s\n", acpi_format_exception(status));
		return -EIO;
	}
	ps_kb_timeout_state = on;
	mutex_unlock(&ps_kb_timeout_lock);
	return 0;
}

static int ps_kb_timeout_get(char *buffer, const struct kernel_param *kp)
{
	int state;

	mutex_lock(&ps_kb_timeout_lock);
	state = ps_kb_timeout_state;
	mutex_unlock(&ps_kb_timeout_lock);
	return scnprintf(buffer, PAGE_SIZE, "%s\n", state < 0 ? "-" : state ? "1" : "0");
}

static const struct kernel_param_ops ps_kb_timeout_ops = {
	.set = ps_kb_timeout_set,
	.get = ps_kb_timeout_get,
};
module_param_cb(kb_timeout, &ps_kb_timeout_ops, NULL, 0644);
MODULE_PARM_DESC(kb_timeout, "Keyboard backlight turns off after 30 s idle: 1 or 0 (\"-\" until set)");
