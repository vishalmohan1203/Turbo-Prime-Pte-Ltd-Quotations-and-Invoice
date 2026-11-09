# Quotation Manager – Turbo Prime Pte Ltd

## Setup (once)
    pip install -r requirements.txt

## Run
    python app.py
Open http://127.0.0.1:5000 in your browser.

## Notes
- Data is stored in `quotations.db` (SQLite) next to app.py. Back this file up.
- Numbering continues from `LAST_USED_NO = 200097` at the top of app.py (next = 200098).
- To let colleagues on the office network use it, change the last line of app.py to
  `app.run(host="0.0.0.0", port=5000)` and open http://<this-pc-ip>:5000 from their PCs.

- The letterhead is an image per company: `static/header_pte.png`. Replace the file to change it.
- Customer addresses and T/C types are saved automatically and can be managed from the 'Saved Customers & T/C Types' page.

## Deploying online
See DEPLOY.md. Set the APP_PASSWORD environment variable so only your team can log in.

## Job orders
Open a quotation and click "Quotation accepted -> Job Order", enter the customer's PO number and the date.
Job order numbers run per year: 26-0001, 26-0002 ... and restart at 27-0001 in 2027.
A quotation that has become a job order is locked (cannot be edited or deleted).

## Updating a live copy
Replace the program files (app.py, templates/, static/) but keep your data folder (DATA_DIR / quotations.db).
New tables are added automatically the first time the app starts.

## Company
The application is used for Turbo Prime Pte Ltd only. The letterhead on all PDFs is edited by an administrator on the **Letterhead** page.
Company names, default currency, "On behalf of" name and default bank text are in the `COMPANIES` block at the top of app.py.

## Tax invoices
Open a delivery order and click "Create tax invoice". Numbers run INV-26-0001, INV-26-0002 ... (change `INV_PREFIX` in app.py).
Items, part numbers and quantities come from the delivery order, unit prices from the job order (matched by description; anything not matched is left 0 for you to fill in).
Everything can be edited on the invoice: PO, vessel, terms, location, items, discount, GST %, and the bank details.

## Bank details
"Bank Details" in the top bar edits the default bank text for each company. A new tax invoice copies its company's default; on the invoice you can change it for that invoice only,
or tick "also save as default". Fill in the SWIFT code / account number once there.

## Job order format
The job order PDF prints only # / Item description / Quantity, then the **Remarks** (same format for both companies). Prices stay on the job order screen
(they feed the tax invoice) but are not printed. The job order screen has no discount fields. Edit the Remarks in the "Remarks" box on the job order screen (one point per line) and save.

## Flow of documents
Quotation -> Job Order -> Purchase Order -> Delivery Order -> Tax Invoice. A flow bar at the top of every document page shows where you are, how many of each
document exist, and links to them. Delivery Orders are created from the Purchase Order page (they still copy the items of the job order); Tax Invoices are made from a delivery order.

## Customer PO number
The customer's PO # on the job order can be changed at any time. The change is also applied to that job order's delivery orders and tax invoices that still show the old
number (blank ones included); a document whose PO # you edited by hand keeps its own. Changing the PO # on a delivery order does the same for its tax invoices.
Job order numbers and purchase order numbers can also be edited; other documents read the current number, so nothing gets out of step.

## Purchase orders
Flow: Quotation -> Job Order -> **Purchase Order** -> **Delivery Order** -> Tax Invoice. A job order points to creating a purchase order; delivery orders are created from the purchase order page.
Open a job order and click "Create purchase order" (also listed under "Purchase Orders" in the top bar). The layout follows the sample PDF (US Letter).
Typed by hand: No, Your Ref, Our Ref No, TC Type and the "To" address; the Date is chosen. Each item has Item description, Part No, Quantity and Unit Price (typed);
Total Price = Quantity x Unit Price, and the TOTAL line is the sum of the item totals, shown in the currency you choose (JPY / EUR / SGD / INR / USD - edit `PO_CURRENCIES` at the top of app.py to add more).
The "To" address can be picked from the saved suppliers and edited; tick "Save this address for future use" to keep it (an existing supplier with the same first line is updated).
Saved suppliers can also be edited / removed on the "Saved Addresses & T/C Types" page.
Purchase orders have no GST line. (Purchase orders saved earlier with GST are changed to Total = sum of the items the first time the new version starts.)

## PDF layout
The Job Order and Purchase Order PDFs follow the sample pages (11pt text, 16pt titles, fixed positions for the details block, table, remarks / totals).

## Latest corrections (Nov 2026)
- **Quotation PDF** follows the latest template (Letter size): Part Num column, Vessel Name line, boxed `TOTAL(currency)` row.
  Quotations and job orders carry Part Num and Vessel Name through to delivery orders.
- **Currencies**: JPY, SGD, INR, EUR, USD on quotations and purchase orders (`CURRENCIES` in app.py).
- **T&C options**: the quotation form offers four Terms & Conditions options (Onboard job / Workshop job / Genuine spares / Compatible spares, from Terms.docx, each starting with "Validity: 30 days"); the text can still be edited per quotation.
  An administrator edits the options under **T&C Options** (starting wording incl. "Validity: 30 days": `TERMS_OPTIONS` in app.py; bumping `TERMS_VERSION` re-issues them in an existing database).
- **Valid till** has been removed from the quotation form, screen and PDF.
- **Job order PDF** has a Vessel Name line; the job order screen no longer shows unit / total price (prices are kept hidden for the tax invoice).
- **Listings** no longer show: Valid till / Total / Status (quotations), Total / Delivery orders (job orders), Total (purchase orders, tax invoices), Tax invoices (delivery orders).
- **Purchase order PDF**: gap between the table headings and the first item.
- **Bank details** are saved but not visible on the tax invoice screen. Only an administrator can open or change them (**Bank Details** page, admin-only);
  the details still print on the invoice PDF. Tick "update existing invoices" there to apply a change to invoices already made.
- Existing databases upgrade automatically on first start (new columns and the T&C options table are added).
- **Bank details per currency** (Turbo Prime Pte Ltd: USD / SGD / JPY, from Bank_Details.docx): editable by an administrator on the Bank Details page.
  The quotation prints the bank details of the currency chosen on it, under the terms & conditions (no bank block for a currency without a set, e.g. INR / EUR).
  A new tax invoice copies the same currency's set, or the company's general details when there is none.


## Documents
- Flow: Quotation -> Job Order -> Delivery Order -> Tax Invoice.
- Stand-alone documents (not part of the flow): Purchase Orders, Commercial Invoices and Packing Lists - each has its own list, "+ New" button and automatic (editable) number.
- Every PDF that runs onto a second page repeats the letterhead at the top of that page.
