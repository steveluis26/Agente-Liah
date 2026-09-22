"""Seed Fase 0: tenant academia-danza-demo + canal sandbox.

Uso: python scripts/seed_fase0.py  (requiere .env con DATABASE_URL apuntando a Postgres).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.core.db import async_session_maker
from app.models import Tenant, TenantConfig, WhatsappChannel


PHONE_NUMBER_ID = "123456789"
VERIFY_TOKEN = "test_token_123"


async def seed():
    async with async_session_maker() as session:
        existing = await session.execute(
            select(Tenant).where(Tenant.slug == "academia-danza-demo")
        )
        if existing.scalar_one_or_none() is not None:
            print("Seed ya existe: academia-danza-demo")
            return

        tenant = Tenant(
            slug="academia-danza-demo",
            name="Academia de Danza Demo",
            business_type="academy",
            timezone="America/Mexico_City",
        )
        session.add(tenant)
        await session.flush()

        config = TenantConfig(
            tenant_id=tenant.id,
            system_prompt=(
                "Eres la asistente virtual de la Academia de Danza Demo. "
                "Tu tono es jovial y cercano. Responde dudas sobre horarios, "
                "precios y agendamiento de clases muestra."
            ),
            tone="jovial",
            business_hours={"mon-fri": ["16:00", "20:00"], "sat": ["10:00", "14:00"]},
            model_routing={"faq": "mini", "booking": "large"},
        )
        session.add(config)

        channel = WhatsappChannel(
            tenant_id=tenant.id,
            bsp="meta",
            phone_number_id=PHONE_NUMBER_ID,
            phone_number="+5215555550000",
            verify_token=VERIFY_TOKEN,
            status="active",
        )
        session.add(channel)
        await session.commit()
        print(f"Seed OK: tenant={tenant.id} phone_number_id={PHONE_NUMBER_ID}")


if __name__ == "__main__":
    asyncio.run(seed())
