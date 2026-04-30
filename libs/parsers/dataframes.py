import re

import polars as pl

from libs.parsers.contacts import parse_clean_name_remove_credentials


def df_clean_names(
    df: pl.DataFrame,
    first_name_col: str,
    last_name_col: str,
) -> pl.DataFrame:
    """Clean first and last names in any dataframe."""
    if {first_name_col, last_name_col} - set(df.columns):
        return df

    return (
        df.with_columns(
            [
                pl.col(col)
                .map_elements(parse_clean_name_remove_credentials, return_dtype=pl.Utf8)
                .alias(f"clean_{col}")
                for col in [first_name_col, last_name_col]
            ],
        )
        .with_columns(
            pl.col(f"clean_{last_name_col}").str.split(" ").alias("last_parts"),
        )
        .with_columns(
            pl.col(f"clean_{first_name_col}").str.split(" ").alias("first_parts"),
        )
        .with_columns(
            [
                pl.when(pl.col("last_parts").list.len() > 0)
                .then(pl.col("last_parts").list.get(-1))
                .when(
                    (
                        pl.col(f"clean_{last_name_col}").is_null()
                        | (pl.col(f"clean_{last_name_col}") == "")
                    )
                    & (pl.col("first_parts").list.len() > 1),
                )
                .then(pl.col("first_parts").list.get(-1))
                .otherwise(None)
                .alias(last_name_col),
                pl.when(pl.col("last_parts").list.len() > 1)
                .then(
                    pl.concat_str(
                        [
                            pl.col(f"clean_{first_name_col}"),
                            pl.lit(" "),
                            pl.col("last_parts").list.slice(0, -1).list.join(" "),
                        ],
                        separator="",
                    ).str.strip_chars(),
                )
                .when(
                    (
                        pl.col(f"clean_{last_name_col}").is_null()
                        | (pl.col(f"clean_{last_name_col}") == "")
                    )
                    & (pl.col("first_parts").list.len() > 1),
                )
                .then(pl.col("first_parts").list.slice(0, -1).list.join(" "))
                .otherwise(pl.col(f"clean_{first_name_col}"))
                .alias(first_name_col),
            ],
        )
        .drop(
            [
                f"clean_{first_name_col}",
                f"clean_{last_name_col}",
                "last_parts",
                "first_parts",
            ],
        )
    )


def parse_pomona_class_year(
    school_name: str | None,
    end_date: str | None,
) -> str | None:
    """Extract class year from end date for Pomona College alumni."""
    if not school_name or not end_date:
        return None

    if "Pomona College" not in str(school_name):
        return None

    year_match = re.search(r"\b(19|20)\d{2}\b", str(end_date))
    match year_match:
        case None:
            return None

        case _:
            return year_match.group(0)


def parse_clay_names(df: pl.DataFrame) -> pl.DataFrame:
    """Clean first and last names in Clay data."""
    match (
        "First Name (2)" in df.columns and "Last Name (2)" in df.columns,
        "First Name" in df.columns and "Last Name" in df.columns,
    ):
        case (True, _):
            return df_clean_names(df, "First Name (2)", "Last Name (2)")

        case (False, True):
            return df_clean_names(df, "First Name", "Last Name")

        case _:
            return df


def _extract_pomona_class_year_from_row(row: dict[str, str | None]) -> str | None:
    return parse_pomona_class_year(
        row.get("School Name - Education"),
        row.get("End Date - Education"),
    )


def parse_pomona_class_years(df: pl.DataFrame) -> pl.DataFrame:
    """Extract class years for Pomona College alumni from Clay data."""
    if (
        "School Name - Education" not in df.columns
        or "End Date - Education" not in df.columns
    ):
        return df.with_columns(pl.lit(None).alias("Class Year"))

    return df.with_columns(
        pl.struct(["School Name - Education", "End Date - Education"])
        .map_elements(
            _extract_pomona_class_year_from_row,
            return_dtype=pl.Utf8,
        )
        .alias("Class Year"),
    )
