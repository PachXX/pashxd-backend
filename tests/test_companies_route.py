"""Integration tests for /api/crm/companies and company-grouped pipeline."""
import pytest

from tests.conftest import login

pytestmark = pytest.mark.asyncio


async def _create_contact(client, name, email, company=""):
    res = await client.post("/api/crm/contacts", json={"name": name, "email": email, "company": company})
    assert res.status_code == 200
    return res.json()


async def test_same_domain_contacts_group_into_one_company(client):
    await login(client)
    await _create_contact(client, "Khalid", "khalid@acme.com", "Acme Trading Est.")
    await _create_contact(client, "Sara", "sara@acme.com", "Acme Trading")

    res = await client.get("/api/crm/companies")
    assert res.status_code == 200
    companies = res.json()["companies"]
    assert len(companies) == 1
    assert companies[0]["contact_count"] == 2


async def test_company_delete_guard(client):
    await login(client)
    contact = await _create_contact(client, "Jane", "jane@beta.com", "Beta Corp")
    company_id = contact["company_id"]

    res = await client.delete(f"/api/crm/companies/{company_id}")
    assert res.status_code == 400

    await client.delete(f"/api/crm/contacts/{contact['id']}")
    res = await client.delete(f"/api/crm/companies/{company_id}")
    assert res.status_code == 200


async def test_company_detail_nests_contacts_and_deals(client):
    await login(client)
    contact = await _create_contact(client, "Omar", "omar@gulftech.ae", "GulfTech Solutions")
    company_id = contact["company_id"]

    deal_res = await client.post("/api/crm/deals", json={
        "title": "GulfTech - Demo", "contact_id": contact["id"], "value": 5000, "stage": "proposal",
    })
    assert deal_res.status_code == 200
    assert deal_res.json()["company_id"] == company_id

    res = await client.get(f"/api/crm/companies/{company_id}")
    assert res.status_code == 200
    body = res.json()
    assert len(body["contacts"]) == 1
    assert len(body["deals"]) == 1
    assert body["company"]["stage"] == "proposal"


async def test_pipeline_places_company_at_most_advanced_live_stage(client):
    await login(client)
    c1 = await _create_contact(client, "Person One", "one@multideal.com", "MultiDeal Inc")
    c2 = await _create_contact(client, "Person Two", "two@multideal.com", "MultiDeal Inc")

    await client.post("/api/crm/deals", json={"title": "Deal A", "contact_id": c1["id"], "stage": "lead"})
    await client.post("/api/crm/deals", json={"title": "Deal B", "contact_id": c2["id"], "stage": "proposal"})

    res = await client.get("/api/crm/pipeline")
    pipeline = res.json()

    assert len(pipeline["proposal"]) == 1
    card = pipeline["proposal"][0]
    assert card["deal_count"] == 2  # full deal list still present, not hidden
    assert len(pipeline["lead"]) == 0  # not duplicated into the lower stage


async def test_pipeline_only_lost_when_all_deals_lost(client):
    await login(client)
    c1 = await _create_contact(client, "P1", "p1@alllost.com", "AllLost Co")
    c2 = await _create_contact(client, "P2", "p2@alllost.com", "AllLost Co")

    d1 = await client.post("/api/crm/deals", json={"title": "D1", "contact_id": c1["id"], "stage": "proposal"})
    d2 = await client.post("/api/crm/deals", json={"title": "D2", "contact_id": c2["id"], "stage": "lead"})

    res = await client.get("/api/crm/pipeline")
    assert len(res.json()["proposal"]) == 1  # one live deal keeps it out of lost

    d1_id = d1.json()["id"]
    d2_id = d2.json()["id"]
    await client.put(f"/api/crm/deals/{d1_id}", json={"stage": "lost"})
    await client.put(f"/api/crm/deals/{d2_id}", json={"stage": "lost"})

    res = await client.get("/api/crm/pipeline")
    pipeline = res.json()
    assert len(pipeline["lost"]) == 1
    assert pipeline["lost"][0]["deal_count"] == 2
    assert all(len(pipeline[s]) == 0 for s in ("lead", "qualified", "proposal", "negotiation", "won"))
