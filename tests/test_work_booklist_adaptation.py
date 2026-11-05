"""Ordinary booklist V2 metadata validation."""

import pytest
from fastapi import HTTPException
from work.work_booklist_service import WorkBooklistService


@pytest.mark.parametrize(
    "body",
    [
        {"title": " "},
        {"title": "x" * 81},
        {"description": "x" * 601},
        {"cover_image_url": "javascript:alert(1)"},
        {"cover_image_url": "/relative.png"},
        {"display_type": True},
        {"display_type": 3},
        {"is_public": "false"},
        {"owner_id": "other"},
        {"is_tournament": True},
    ],
)
def test_invalid_fields(body):
    with pytest.raises(HTTPException) as error:
        WorkBooklistService.validate_fields(body)
    assert error.value.status_code == 422


def test_trim_and_display_defaults():
    assert WorkBooklistService.validate_fields(
        {"title": " hi ", "display_type": 2}
    ) == {
        "title": "hi",
        "default_sort_method": "display_order",
        "default_sort_order": "asc",
    }
    assert WorkBooklistService.validate_fields({"cover_image_url": None}) == {
        "cover_image_url": None
    }


@pytest.mark.parametrize(
    "body",
    [
        {"comment": 5},
        {"comment": "x" * 801},
        {"display_order": True},
        {"display_order": 2**31},
        {"work_id": 1},
    ],
)
def test_invalid_item_fields(body):
    with pytest.raises(HTTPException):
        WorkBooklistService.validate_item(body)
