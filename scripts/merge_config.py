#!/usr/bin/env python3
"""Deep-merge a YAML override file into the frame config (stdlib + PyYAML).

Usage: merge_config.py <config_frame.yaml> <override.yaml>

Used by setup_pi.sh to apply `mimir.yaml` from the boot partition on first boot
— the way to pick the hardware variant (`variant: eink`) or pre-seed settings
before the frame has ever been on a network. Dicts merge recursively; every
other value (lists included) is replaced by the override.
"""
import sys

import yaml


def deep_merge(base, over):
    for key, val in (over or {}).items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], val)
        else:
            base[key] = val
    return base


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    cfg_path, over_path = argv[1], argv[2]
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}
    with open(over_path) as f:
        over = yaml.safe_load(f) or {}
    if not isinstance(over, dict):
        print("override is not a mapping: %s" % over_path)
        return 1
    deep_merge(cfg, over)
    with open(cfg_path, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False)
    print("merged keys: %s" % ', '.join(sorted(over)))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
