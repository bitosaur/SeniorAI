-- SeniorAI — audio priority rules for NUC6CAYH production box.
--
-- Goal: on every boot, make PipeWire default to:
--   * OUTPUT  → Dell AC511 USB Sound Bar (vendor 413c:a503)
--   * INPUT   → Medeli USB FS AUDIO mic  (vendor 0a67:d034)
--
-- and actively de-prioritize:
--   * the onboard Intel HDA / ALC283 (pci 0000:00:0e.0)
--   * the Dell soundbar's own mic jack (we ignore it per hardware plan)
--   * the USB camera's built-in mic (Sunplus SPCA2281)
--
-- Installed to /etc/wireplumber/main.lua.d/51-seniorai-audio.lua by
-- scripts/install_linux.sh. After installing, restart WirePlumber:
--
--     systemctl --user restart wireplumber
--
-- WirePlumber evaluates rules in filename-alphabetical order; 51- keeps us
-- well after the 50-default rules so our priorities win. This is Lua config
-- for WirePlumber 0.4.x (Debian 12 Bookworm default).

-- Prefer Dell AC511 soundbar as the default sink.
table.insert(alsa_monitor.rules, {
  matches = {
    { { "node.name", "matches", "alsa_output.usb-Dell_Dell_AC511_USB_SoundBar*" } },
  },
  apply_properties = {
    ["priority.session"] = 2000,
    ["priority.driver"]  = 2000,
  },
})

-- Prefer Medeli USB FS AUDIO as the default source.
table.insert(alsa_monitor.rules, {
  matches = {
    { { "node.name", "matches", "alsa_input.usb-USB_FS_AUDIO_USB_FS_AUDIO*" } },
  },
  apply_properties = {
    ["priority.session"] = 2000,
    ["priority.driver"]  = 2000,
  },
})

-- De-prioritize everything we never want as a default.
table.insert(alsa_monitor.rules, {
  matches = {
    { { "node.name", "matches", "alsa_output.pci-0000_00_0e.0.*" } },
    { { "node.name", "matches", "alsa_input.pci-0000_00_0e.0.*"  } },
    { { "node.name", "matches", "alsa_input.usb-Dell_Dell_AC511_USB_SoundBar*" } },
    { { "node.name", "matches", "alsa_input.usb-FPL-ZW-230921*" } },
  },
  apply_properties = {
    ["priority.session"] = 10,
    ["priority.driver"]  = 10,
  },
})
