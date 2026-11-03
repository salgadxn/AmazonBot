# Wholesale price lists (optional)

Drop CSV files from distributors/wholesalers here. Required columns:

| upc | cost | supplier |
|---|---|---|
| 078742000000 | 4.25 | KeHE |

Optional columns: `name`, `link`, `case_pack`. Cost = your price per unit.
The bot matches each UPC to its Amazon listing, checks eligibility, and
adds profitable ones to the **Leads** tab. Wholesale invoices are also the
best documents for ungating gated brands.
