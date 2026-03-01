# routers/company.py — Company Profile (Tenant master)
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Tenant
from utils.auth import get_tenant_payload as get_current_user_payload

router = APIRouter()


class CompanyProfileUpdate(BaseModel):
    company_name:      Optional[str] = None
    gstin:             Optional[str] = None
    pan:               Optional[str] = None
    phone:             Optional[str] = None
    email:             Optional[str] = None
    address:           Optional[str] = None
    state:             Optional[str] = None
    logo_url:          Optional[str] = None   # base64 data URL
    qr_code_url:       Optional[str] = None   # UPI QR base64 data URL
    upi_id:            Optional[str] = None
    bank_name:         Optional[str] = None
    bank_account:      Optional[str] = None
    bank_ifsc:         Optional[str] = None
    bank_branch:       Optional[str] = None
    authorised_person: Optional[str] = None
    terms_conditions:  Optional[str] = None


@router.get("/")
async def get_company_profile(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Return the current tenant's company profile."""
    tenant = await db.get(Tenant, payload["tenant_id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    return {
        "id":                tenant.id,
        "company_name":      tenant.company_name,
        "gstin":             tenant.gstin,
        "pan":               getattr(tenant, "pan", None),
        "phone":             tenant.phone,
        "email":             tenant.email,
        "address":           tenant.address,
        "state":             tenant.state,
        "logo_url":          tenant.logo_url,
        "qr_code_url":       getattr(tenant, "qr_code_url", None),
        "upi_id":            getattr(tenant, "upi_id", None),
        "bank_name":         getattr(tenant, "bank_name", None),
        "bank_account":      getattr(tenant, "bank_account", None),
        "bank_ifsc":         getattr(tenant, "bank_ifsc", None),
        "bank_branch":       getattr(tenant, "bank_branch", None),
        "authorised_person": getattr(tenant, "authorised_person", None),
        "terms_conditions":  getattr(tenant, "terms_conditions", None),
        "plan":              tenant.plan.value,
    }


@router.put("/")
async def update_company_profile(
    body:    CompanyProfileUpdate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Update the current tenant's company profile."""
    tenant = await db.get(Tenant, payload["tenant_id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    for field, value in body.dict(exclude_unset=True).items():
        if hasattr(tenant, field):
            setattr(tenant, field, value)

    await db.commit()
    await db.refresh(tenant)

    return {"message": "Company profile updated successfully"}
