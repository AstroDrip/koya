from pydantic import BaseModel, Field
from typing import Literal, Optional

class CustomerFields(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    address: Optional[str] = Field(default=None, max_length=300)
    phone: Optional[str] = Field(default=None, max_length=30)
    pincode: Optional[str] = Field(default=None, max_length=12)

class ProductLookup(BaseModel):
    product_name: Optional[str] = Field(default=None, max_length=120)
    model_number: Optional[str] = Field(default=None, max_length=120)
    serial_number: Optional[str] = Field(default=None, max_length=120)
    warranty_number: Optional[str] = Field(default=None, max_length=120)

class PriceLookup(BaseModel):
    product_name: str = Field(min_length=1, max_length=120)
    issue_code: str = Field(min_length=1, max_length=80)

class RequestCreate(BaseModel):
    request_type: Literal["complaint", "callback", "appointment"]
    customer_name: str = Field(min_length=1, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=30)
    address: Optional[str] = Field(default=None, max_length=300)
    pincode: Optional[str] = Field(default=None, max_length=12)
    product_name: Optional[str] = Field(default=None, max_length=120)
    model_number: Optional[str] = Field(default=None, max_length=120)
    serial_number: Optional[str] = Field(default=None, max_length=120)
    invoice_number: Optional[str] = Field(default=None, max_length=120)
    warranty_number: Optional[str] = Field(default=None, max_length=120)
    issue_description: Optional[str] = Field(default=None, max_length=2000)
    requested_date: Optional[str] = Field(default=None, max_length=30)
    requested_time: Optional[str] = Field(default=None, max_length=30)
    customer_confirmed: bool = False
    interaction_id: Optional[str] = Field(default=None, max_length=150)
    idempotency_key: Optional[str] = Field(default=None, max_length=180)
