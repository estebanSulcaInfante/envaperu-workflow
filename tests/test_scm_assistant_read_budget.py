from sqlalchemy import create_engine, Integer, ForeignKey, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, Session, selectinload
import pytest
from app.services.scm_assistant_catalogue import bounded_orm_reads, CatalogueQueryError

class Base(DeclarativeBase): pass
class Parent(Base):
    __tablename__='budget_parent'
    id:Mapped[int]=mapped_column(primary_key=True)
    children:Mapped[list['Child']]=relationship()
class Child(Base):
    __tablename__='budget_child'
    id:Mapped[int]=mapped_column(primary_key=True)
    parent_id:Mapped[int]=mapped_column(ForeignKey('budget_parent.id'))

@pytest.fixture
def session():
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all(Parent(id=i) for i in range(1,502));s.add(Child(id=1,parent_id=1));s.commit()
        yield s
    engine.dispose()

def test_budget_preserves_scalar_limit_and_relationship_loads(session):
    with bounded_orm_reads(session):
        assert session.scalar(select(Parent).order_by(Parent.id).limit(1)).id==1
        assert len(session.scalars(select(Parent).limit(2)).all())==2
        parents=session.scalars(select(Parent).where(Parent.id==1).options(selectinload(Parent.children))).unique().all()
        assert parents[0].children[0].id==1
        assert session.execute(text('SELECT 1')).scalar()==1
        assert len(session.scalars(select(Parent).limit(500)).all())==500

def test_budget_rejects_501_and_removes_listener_after_error(session):
    with pytest.raises(CatalogueQueryError) as error:
        with bounded_orm_reads(session):session.scalars(select(Parent)).all()
    assert error.value.code=='ASSISTANT_LIMIT_EXCEEDED'
    assert len(session.scalars(select(Parent)).all())==501

def test_budget_caps_internal_query_count(session):
    with pytest.raises(CatalogueQueryError):
        with bounded_orm_reads(session):
            for _ in range(201):session.scalar(select(Parent.id).limit(1))
