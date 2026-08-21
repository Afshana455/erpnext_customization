import frappe
import requests
from frappe import _
from frappe.utils import flt, now_datetime

INTEGRATION_SITE = (
    "http://bank_integration.site:8000"
)

def get_integration_headers():

    credentials = frappe.get_single(
        "Integration Site Keys"
    )

    api_key = credentials.api_key
    api_secret = credentials.secret_key

    if not api_key or not api_secret:
        frappe.throw(
            "Integration API credentials "
            "are not configured."
        )

    return {
        "Authorization":
            f"token {api_key}:{api_secret}",

        "Content-Type":
            "application/json",

        "Accept":
            "application/json"
    }

def call_integration_api(
    method,
    payload
):

    url = (
        f"{INTEGRATION_SITE}"
        f"/api/method/{method}"
    )

    try:

        response = requests.post(
            url,
            json=payload,
            headers=get_integration_headers(),
            timeout=15
        )

    except requests.Timeout:

        frappe.log_error(
            title="Integration Timeout",
            message=frappe.get_traceback()
        )

        frappe.throw(
            "Bank integration site "
            "did not respond in time."
        )

    except requests.RequestException:

        frappe.log_error(
            title="Integration Connection Error",
            message=frappe.get_traceback()
        )

        frappe.throw(
            "Unable to connect to "
            "bank integration site."
        )

    if response.status_code not in (
        200,
        202
    ):
        frappe.log_error(
            title="Integration API Error",
            message=(
                f"HTTP Status: "
                f"{response.status_code}\n\n"
                f"{response.text}"
            )
        )

        try:
            error_data = response.json()

            error_message = (
                error_data.get("message")
                or error_data.get("exception")
                or response.text
            )

        except ValueError:
            error_message = response.text

        frappe.throw(
            "Bank integration site returned "
            f"HTTP {response.status_code}: "
            f"{error_message}"
        )

    try:

        result = response.json()

    except ValueError:

        frappe.throw(
            "Bank integration site returned "
            "an invalid response."
        )

    data = result.get("message")

    if not isinstance(data, dict):
        frappe.throw(
            "Bank integration site returned "
            "invalid payment data."
        )

    return data


@frappe.whitelist()
def create_bank_payment_request(
    invoice,
    source_account,
    supplier_account,
    mode_of_payment
):

    if not invoice:
        frappe.throw(_("Purchase Invoice is required."))

    if not source_account:
        frappe.throw(_("Company bank account is required."))

    if not supplier_account:
        frappe.throw(_("Supplier bank account is required."))

    if not mode_of_payment:
        frappe.throw(_("Mode of payment is required."))

    purchase_invoice = frappe.get_doc(
        "Purchase Invoice",
        invoice
    )

    if purchase_invoice.docstatus != 1:
        frappe.throw(
            _("Only submitted Purchase Invoices can be paid.")
        )

    # --------------------------------------------------------
    # Calculate custom outstanding amount
    # --------------------------------------------------------

    completed_amount = 0

    for row in (
        purchase_invoice.custom_payment_details
        or []
    ):

        if row.payment_status == "Completed":
            completed_amount += flt(
                row.amount
            )

    outstanding_amount = max(
        flt(purchase_invoice.grand_total or 0)
        - completed_amount,
        0
    )

    if outstanding_amount <= 0:
        frappe.throw(
            _("This Purchase Invoice has no outstanding amount.")
        )

    # --------------------------------------------------------
    # Prevent multiple active payments
    # --------------------------------------------------------

    active_statuses = [
        "OTP Pending",
        "OTP Verified",
        "Payment Initiated",
        "Pending"
    ]

    for row in (
        purchase_invoice.custom_payment_details
        or []
    ):

        if row.payment_status in active_statuses:

            frappe.throw(
                _(
                    "There is already an active payment "
                    "request for this Purchase Invoice."
                )
            )

    # --------------------------------------------------------
    # Get supplier account number
    # --------------------------------------------------------

    supplier_account_number = frappe.db.get_value(
        "Bank Account",
        supplier_account,
        "bank_account_no"
    )

    if not supplier_account_number:
        supplier_account_number = supplier_account

    # --------------------------------------------------------
    # Get company account number
    # --------------------------------------------------------

    source_account_number = frappe.db.get_value(
        "Bank Account",
        source_account,
        "bank_account_no"
    )

    if not source_account_number:
        source_account_number = source_account

    # --------------------------------------------------------
    # Call bank integration
    # --------------------------------------------------------

    response = call_integration_api(
        "bank_integration.api.payment.create_payment_request",
        {
            "erp_site":
                frappe.local.site,

            "erp_doctype":
                "Purchase Invoice",

            "erp_document_name":
                purchase_invoice.name,

            "currency":
                purchase_invoice.currency,

            "amount":
                outstanding_amount,

            "source_account":
                source_account_number,

            "beneficiary_account":
                supplier_account_number,

            "mode_of_payment":
                mode_of_payment
        }
    )

    if not response:
        frappe.throw(
            _("No response received from bank integration.")
        )

    if not response.get("success"):
        frappe.throw(
            response.get(
                "message",
                _("Unable to create bank payment request.")
            )
        )

    request_id = response.get(
        "request_id"
    )

    if not request_id:
        frappe.throw(
            _("Bank integration did not return a request ID.")
        )

    # --------------------------------------------------------
    # Add Payment Details row
    # --------------------------------------------------------

    payment_row = purchase_invoice.append(
        "custom_payment_details",
        {}
    )

    payment_row.payment_id = request_id

    payment_row.amount = outstanding_amount

    payment_row.currency = (
        response.get("currency")
        or purchase_invoice.currency
    )

    payment_row.outstanding_amount = (
        outstanding_amount
    )

    payment_row.payment_date = (
        now_datetime().date()
    )

    payment_row.payment_status = (
        "OTP Pending"
    )

    purchase_invoice.save(
        ignore_permissions=True
    )

    frappe.db.commit()

    return {
        "success": True,

        "request_id":
            request_id,

        "supplier_account":
            supplier_account_number,

        "source_account":
            source_account_number,

        "mode_of_payment":
            mode_of_payment,

        "currency":
            purchase_invoice.currency,

        "amount":
            outstanding_amount

    }


# ============================================================
# VERIFY OTP
# ============================================================

@frappe.whitelist()
def validate_bank_payment_otp(
    invoice,
    payment_request,
    otp
):

    if not invoice:
        frappe.throw(
            _("Purchase Invoice is required.")
        )

    if not payment_request:
        frappe.throw(
            _("Payment request is required.")
        )

    if not otp:
        frappe.throw(
            _("OTP is required.")
        )

    response = call_integration_api(
        "bank_integration.api.otp.validate_payment_otp",
        {
            "payment_request":
                payment_request,

            "entered_otp":
                otp
        }
    )

    if not response:
        return {
            "success": False,
            "message":
                _("No response from bank integration.")
        }

    # --------------------------------------------------------
    # Update ERP Payment Details
    # --------------------------------------------------------

    purchase_invoice = frappe.get_doc(
        "Purchase Invoice",
        invoice
    )

    payment_row = None

    for row in (
        purchase_invoice.custom_payment_details
        or []
    ):

        if row.payment_id == payment_request:
            payment_row = row
            break

    if payment_row:

        otp_status = response.get(
            "otp_status"
        )

        if otp_status == "Verified":

            payment_row.payment_status = (
                "OTP Verified"
            )

        elif otp_status == "Invalid":

            payment_row.payment_status = (
                "OTP Pending"
            )

        purchase_invoice.save(
            ignore_permissions=True
        )

    frappe.db.commit()

    return response


# ============================================================
# SUBMIT BANK PAYMENT
# ============================================================

@frappe.whitelist()
def submit_bank_payment(
    invoice,
    payment_request,
    amount
):

    if not invoice:
        frappe.throw(
            _("Purchase Invoice is required.")
        )

    if not payment_request:
        frappe.throw(
            _("Payment request is required.")
        )

    amount = flt(amount)

    if amount <= 0:
        frappe.throw(
            _("Payment amount must be greater than zero.")
        )

    purchase_invoice = frappe.get_doc(
        "Purchase Invoice",
        invoice
    )

    # --------------------------------------------------------
    # Find Payment Details row
    # --------------------------------------------------------

    payment_row = None

    for row in (
        purchase_invoice.custom_payment_details
        or []
    ):

        if row.payment_id == payment_request:
            payment_row = row
            break

    if not payment_row:
        frappe.throw(
            _(
                "Payment Details row not found for "
                "payment request {0}."
            ).format(payment_request)
        )

    if payment_row.payment_status != "OTP Verified":
        frappe.throw(
            _("OTP must be verified before submitting payment.")
        )

    # --------------------------------------------------------
    # Validate outstanding amount
    # --------------------------------------------------------

    completed_amount = 0

    for row in (
        purchase_invoice.custom_payment_details
        or []
    ):

        if row.payment_status == "Completed":
            completed_amount += flt(
                row.amount
            )

    outstanding_amount = max(
        flt(purchase_invoice.grand_total or 0)
        - completed_amount,
        0
    )

    if amount > outstanding_amount:
        frappe.throw(
            _(
                "Payment amount cannot exceed "
                "the outstanding amount."
            )
        )

    # --------------------------------------------------------
    # Call bank integration
    # --------------------------------------------------------

    response = call_integration_api(
        "bank_integration.api.payment.submit_payment_request",
        {
            "payment_request":
                payment_request,

            "amount":
                amount
        }
    )

    if not response:
        frappe.throw(
            _("No response received from bank integration.")
        )

    if not response.get("success"):
        frappe.throw(
            response.get(
                "response_message",
                response.get(
                    "message",
                    _("Bank payment submission failed.")
                )
            )
        )

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Initially transaction_id = request ID.
    #
    # Scheduler will replace it with the real
    # bank transaction ID later.
    # --------------------------------------------------------

    transaction_id = payment_request

    transaction_log = frappe.get_doc({
        "doctype":
            "Bank Transaction Log",

        "transaction_id":
            transaction_id,

        "transaction_date":
            now_datetime(),

        "status":
            "Initiated",

        "payment_request_id":
            payment_request,

        "erp_site":
            frappe.local.site,

        "erp_doctype":
            "Purchase Invoice",

        "erp_document":
            purchase_invoice.name,

        "invoice_amount":
            flt(purchase_invoice.grand_total),

        "transaction_amount":
            amount,

        "currency":
            purchase_invoice.currency,

        "mode_of_payment":
            response.get(
                "mode_of_payment"
            ) or payment_row.get(
                "mode_of_payment"
            ) or "",

        "otp_status":
            "Verified",

        "otp_verified_at":
            response.get(
                "otp_verified_at"
            ),

        "bank_response_code":
            response.get(
                "response_code"
            ) or "",

        "bank_response_message":
            response.get(
                "response_message"
            ) or "",

        "bank_response_timestamp":
            response.get(
                "response_timestamp"
            )
    })

    transaction_log.insert(
        ignore_permissions=True
    )

    # --------------------------------------------------------
    # Update Payment Details
    # --------------------------------------------------------

    payment_row.amount = amount

    payment_row.payment_status = "Pending"


    payment_row.payment_date = (
        now_datetime().date()
    )

    purchase_invoice.save(
        ignore_permissions=True
    )

    frappe.db.commit()

    return {
        "success": True,

        "payment_status":
            "Payment Initiated",

        "request_id":
            payment_request,

        "transaction_id":
            transaction_id,

        "amount":
            amount
    }


# ============================================================
# UPDATE PURCHASE INVOICE PAYMENT
# ============================================================

def update_purchase_invoice_payment(
    log,
    payment_status,
    response
):

    if not log.erp_document:

        frappe.log_error(
            "ERP document is missing from Bank Transaction Log.",
            "Bank Payment Status Update"
        )

        return

    purchase_invoice = frappe.get_doc(
        "Purchase Invoice",
        log.erp_document
    )

    payment_row = None

    for row in (
        purchase_invoice.custom_payment_details
        or []
    ):

        if row.payment_id == log.payment_request_id:

            payment_row = row

            break

    if not payment_row:

        frappe.log_error(
            (
                f"Payment Details row not found for "
                f"payment request {log.payment_request_id} "
                f"on Purchase Invoice "
                f"{purchase_invoice.name}."
            ),
            "Bank Payment Status Update"
        )

        return

    # --------------------------------------------------------
    # Update payment status
    # --------------------------------------------------------

    payment_row.payment_status = payment_status

    # --------------------------------------------------------
    # Update amount
    # --------------------------------------------------------

    if response.get("amount") is not None:

        payment_row.amount = flt(
            response.get("amount")
        )

    # --------------------------------------------------------
    # Update currency
    # --------------------------------------------------------

    if response.get("currency"):

        payment_row.currency = (
            response.get("currency")
        )

    # --------------------------------------------------------
    # Update outstanding amount
    # --------------------------------------------------------

    completed_amount = 0

    for row in (
        purchase_invoice.custom_payment_details
        or []
    ):

        if row.payment_status == "Completed":

            completed_amount += flt(
                row.amount
            )

    payment_row.outstanding_amount = max(
        flt(purchase_invoice.grand_total or 0)
        - completed_amount,
        0
    )

    purchase_invoice.save(
        ignore_permissions=True
    )