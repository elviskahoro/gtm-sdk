"""ETL script for marketplace products.

This module parses marketplace_products.csv and converts each row into a Pydantic BaseModel.
Provides helper functions that can be called by other scripts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ibis
import narwhals as nw
from pydantic import BaseModel, Field


class MarketplaceProduct(BaseModel):
    """Pydantic model representing a marketplace product."""

    title: str = Field(
        description="Product title",
    )
    id: str = Field(
        description="Product ID",
    )
    product_category_id: str = Field(
        description="Product category UUID",
    )
    hid: int = Field(
        description="Product HID",
    )
    product_category_hid: int = Field(
        description="Product category HID",
    )
    description: str | None = None


def df_load_products(
    csv_path: str | Path,
) -> nw.DataFrame[Any]:
    """Load marketplace products from CSV into a Narwhals DataFrame.

    Args:
        csv_path: Path to the marketplace_products.csv file

    Returns:
        Narwhals DataFrame containing all products
    """
    table = ibis.read_csv(csv_path)
    return nw.from_native(table.to_pyarrow(), eager_only=True)


def parse_csv_row(
    row: dict[str, Any],
) -> MarketplaceProduct:
    """Parse a single CSV row into a MarketplaceProduct model.

    Args:
        row: Dictionary representing a single row from the CSV

    Returns:
        MarketplaceProduct instance
    """
    product = MarketplaceProduct(
        title=str(row["title"]),
        id=str(row["id"]),
        product_category_id=str(row["product_category_id"]),
        hid=int(row["hid"]),
        product_category_hid=int(row["product_category_hid"]),
    )

    return product


def load_products_as_models(
    csv_path: str | Path,
) -> list[MarketplaceProduct]:
    """Load all products from CSV as Pydantic models.

    Args:
        csv_path: Path to the marketplace_products.csv file

    Returns:
        List of MarketplaceProduct instances
    """
    df = df_load_products(
        csv_path=csv_path,
    )

    products = []

    for row_dict in df.iter_rows(named=True):
        product = parse_csv_row(
            row=row_dict,
        )
        products.append(product)

    return products


def get_products_by_category(
    csv_path: str | Path,
    category_id: str,
) -> list[MarketplaceProduct]:
    """Get all products belonging to a specific category.

    Args:
        csv_path: Path to the marketplace_products.csv file
        category_id: Product category UUID to filter by

    Returns:
        List of MarketplaceProduct instances matching the category
    """
    df = df_load_products(
        csv_path=csv_path,
    )

    filtered_df = df.filter(nw.col("product_category_id") == category_id)

    products = []

    for row_dict in filtered_df.iter_rows(named=True):
        product = parse_csv_row(
            row=row_dict,
        )
        products.append(product)

    return products


def main() -> None:
    """Main execution function."""
    csv_path = (
        Path(__file__).parent.parent.parent.parent / "data" / "marketplace_products.csv"
    )

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found at {csv_path}")

    print(f"Loading products from {csv_path}")

    products = load_products_as_models(
        csv_path=csv_path,
    )
    print(f"Converted {len(products)} rows to MarketplaceProduct models")

    if products:
        print(f"\nFirst product: {products[0].title}")
        print(f"Product ID: {products[0].id}")
        print(f"Category ID: {products[0].product_category_id}")


if __name__ == "__main__":
    main()
