import sys
import time

from browser import CrmBrowser


def main() -> int:
    stamp = int(time.time())
    first, last = "Ivan", f"Petrenko{stamp % 10000}"
    email = f"ivan{stamp % 10000}@example.com"

    crm = CrmBrowser(headless="--headed" not in sys.argv)
    try:
        crm.login()
        crm.open_contact_form()
        crm.fill_field("firstName", first)
        crm.fill_field("lastName", last)
        crm.fill_field("email", email)
        print("form before save:", crm.read_form())

        record_id = crm.save()
        if not record_id:
            print("FAIL: no record id after Save")
            return 1

        checks = crm.verify_saved_contact(record_id, first, last, email)
        print("record id:", record_id)
        print("checks:", checks)
        ok = all(checks.values())
        print("RESULT:", "OK" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        crm.close()


if __name__ == "__main__":
    sys.exit(main())
