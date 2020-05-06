#!/usr/bin/env python

from __future__ import print_function
import os
import sys

import jinja2

FW_UPDATE_APP_PATH="mlnx_fw_update.py"
TEMPLATE_PATH="j2_templates/mellanox_fw_update.yaml.j2"
RENDERED_TEMPLATE_PATH="mellanox_fw_update.yaml"

def main():
    try:
        print("Rendering mellanox_fw_update.yaml...")
        work_dir=sys.path[0]
        # Read python file
        fd = open(os.path.join(work_dir, FW_UPDATE_APP_PATH), 'r')
        lines = fd.readlines()
        fd.close()

        # Read template file
        fd = open(os.path.join(work_dir,TEMPLATE_PATH), 'r')
        template_data = fd.read()
        fd.close()

        # Create template
        template = jinja2.Template(template_data)

        # Render it then write to file
        fd = open(os.path.join(work_dir,RENDERED_TEMPLATE_PATH), 'w')
        fd.write(template.render(mlnx_fw_update=lines))
        # Append newline to EOF
        fd.write('\n')
        fd.close()
        print("Rendered successfully: %s" %
              os.path.join(work_dir,RENDERED_TEMPLATE_PATH))
    except Exception as e:
        print("Error occured: %s" % str(e))
        if not fd.closed:
            fd.close()


if __name__ == "__main__":
    main()
