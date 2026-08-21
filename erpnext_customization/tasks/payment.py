import frappe

from erpnext_customization.api.payment import (
    call_integration_api,
    update_purchase_invoice_payment
)


def sync_bank_payment_status():

    logs = frappe.get_all(
        "Bank Transaction Log",
        filters={
            "status": [
                "in",
                [
                    "Initiated",
                    "Pending"
                ]
            ]
        },
        fields=[
            "name",
            "payment_request_id",
            "erp_document"
        ],
        limit_page_length=50
    )

    for log_data in logs:

        try:

            log = frappe.get_doc(
                "Bank Transaction Log",
                log_data.name
            )

            if not log.payment_request_id:
                continue

            # ------------------------------------------------
            # Ask Bank Integration for latest status
            # ------------------------------------------------

            response = call_integration_api(
                "bank_integration.api.payment.get_payment_status",
                {
                    "payment_request":
                        log.payment_request_id
                }
            )

            if not response:
                continue

            payment_status = response.get(
                "payment_status"
            )

            transaction_id = response.get(
                "transaction_id"
            )

            # ------------------------------------------------
            # Update real transaction ID
            # ------------------------------------------------

            if transaction_id:

                log.transaction_id = (
                    transaction_id
                )

            # ------------------------------------------------
            # Update status
            # ------------------------------------------------

            status_map = {

                "INITIATED":
                    "Initiated",

                "PENDING":
                    "Pending",

                "COMPLETED":
                    "Completed",

                "FAILED":
                    "Failed",

                "REJECTED":
                    "Rejected"
            }

            normalized_status = (
                status_map.get(
                    str(payment_status)
                    .strip()
                    .upper(),
                    log.status
                )
            )

            log.status = normalized_status

            # ------------------------------------------------
            # Update bank response
            # ------------------------------------------------

            if response.get("response_code"):

                log.bank_response_code = (
                    response.get(
                        "response_code"
                    )
                )

            if response.get("response_message"):

                log.bank_response_message = (
                    response.get(
                        "response_message"
                    )
                )

            if response.get(
                "response_timestamp"
            ):

                log.bank_response_timestamp = (
                    response.get(
                        "response_timestamp"
                    )
                )

            if response.get(
                "processed_at"
            ):

                log.processed_at = (
                    response.get(
                        "processed_at"
                    )
                )

            log.save(
                ignore_permissions=True
            )

            # ------------------------------------------------
            # Update Purchase Invoice child table
            # ------------------------------------------------

            update_purchase_invoice_payment(
                log,
                normalized_status,
                response
            )

            frappe.db.commit()

        except Exception:

            frappe.log_error(
                title="ERP Bank Payment Sync Error",
                message=frappe.get_traceback()
            )